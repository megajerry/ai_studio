"""Reviewer / Whistle-blower role + worker-wiring tests (ADR-0003, ADR-0014).

Three layers, mirroring test_retro:

- **Pure logic** — :func:`assess_risks` computes risk signals from FACTS only
  (no DB, no model): flags hallucinated success / over-budget / repeated failures
  / recurring denials / gated 🔴 actions, and passes a clean episode.
- **Worker wiring** — the ``WORKER_REVIEW`` trigger policy (``on_risk`` default |
  ``always`` | ``off``) via an in-memory fake queue, and NO review-loop.
- **Live DB** — ``run_review`` end-to-end: reads the target's real event trail +
  artifact, and **evidence beats a lying model** — a monkeypatched "looks fine"
  model does NOT change the fact-based verdict. High severity escalates (🚨
  ``review.alarm`` + a 🛑 approval row). ``review.*`` events leak no secrets. These
  SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.enforce import MemoryEventSink
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.policy import load_policy
from runtime.roles import reviewer as reviewer_mod
from runtime.roles.reviewer import (
    EVENT_REVIEW_ALARM,
    EVENT_REVIEW_FLAGGED,
    EVENT_REVIEW_PASSED,
    REVIEW_TASK_TYPE,
    ReviewFacts,
    ReviewResult,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_NONE,
    assess_risks,
    run_review,
)
from runtime.roles.verifier import VerifyResult
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import EVENT_REVIEW_TRIGGERED, run_once


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.delenv("WORKER_REVIEW", raising=False)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


# ===========================================================================
# Pure logic — assess_risks (FACTS in, signals out; no DB, no model)
# ===========================================================================


def test_clean_episode_passes():
    facts = ReviewFacts(
        claims_success=True, artifact_expected=True, artifact_checked=True,
        artifact_ok=True, spent_tokens=100, budget_tokens=1000, retries=0,
    )
    assert assess_risks(facts) == []


def test_hallucinated_success_no_matching_artifact_is_high():
    """A done/verified CLAIM whose real artifact lacks the marker → HIGH."""
    facts = ReviewFacts(
        claims_success=True, artifact_expected=True, artifact_checked=True,
        artifact_ok=False,
    )
    signals = assess_risks(facts)
    assert any(s.severity == SEVERITY_HIGH for s in signals)
    assert any("does not back it" in s.reason for s in signals)


def test_hallucinated_success_no_artifact_at_all_is_high():
    facts = ReviewFacts(claims_success=True, artifact_expected=False)
    signals = assess_risks(facts)
    assert signals and signals[0].severity == SEVERITY_HIGH
    assert "no artifact" in signals[0].reason


def test_no_hallucination_flag_when_unverified():
    """If we could NOT read the artifact (no registry), stay silent — UNVERIFIED is
    not a false flag; the reviewer only fires when it can refute from facts."""
    facts = ReviewFacts(
        claims_success=True, artifact_expected=True, artifact_checked=False,
    )
    assert assess_risks(facts) == []


def test_over_budget_is_high():
    facts = ReviewFacts(spent_tokens=1500, budget_tokens=1000)
    signals = assess_risks(facts)
    assert any(s.severity == SEVERITY_HIGH and "exceeded budget" in s.reason for s in signals)


def test_near_budget_is_medium():
    facts = ReviewFacts(spent_tokens=950, budget_tokens=1000)
    signals = assess_risks(facts)
    assert any(s.severity == SEVERITY_MEDIUM and "near budget" in s.reason for s in signals)


def test_repeated_failures_escalate_with_count():
    assert any(s.severity == SEVERITY_MEDIUM
               for s in assess_risks(ReviewFacts(fail_signals=2)))
    assert any(s.severity == SEVERITY_HIGH
               for s in assess_risks(ReviewFacts(fail_signals=3, retries=1)))


def test_recurring_denials_and_gated_actions_flagged():
    assert any("policy denials" in s.reason for s in assess_risks(ReviewFacts(deny_count=2)))
    assert any("approval-gated" in s.reason for s in assess_risks(ReviewFacts(pend_count=2)))


# ===========================================================================
# Worker wiring — WORKER_REVIEW trigger policy + NO review-loop (fake queue)
# ===========================================================================


class FakeQueue:
    """Minimal in-memory queue mirroring enqueue/claim/heartbeat/complete."""

    def __init__(self, sink: MemoryEventSink) -> None:
        self.sink = sink
        self.tasks: dict = {}
        self.order: list = []

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type, status=TaskStatus.QUEUED,
                 priority=priority, payload=payload or {}, created_at=now, updated_at=now,
                 budget_tokens=budget_tokens)
        self.tasks[t.id] = t
        self.order.append(t.id)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order if self.tasks[i].status is TaskStatus.QUEUED
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        ids.sort(key=lambda i: -self.tasks[i].priority)
        t = self.tasks[ids[0]].model_copy(update={
            "status": TaskStatus.IN_PROGRESS, "claimed_by": worker_id,
            "heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[t.id] = t
        return t

    def heartbeat(self, conn, task_id, worker_id):
        return self.tasks.get(task_id)

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.DONE,
                 spent_tokens=None, force=False):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"status": status, "result": result})
        self.tasks[task_id] = t
        return t

    def queued_of_type(self, type_: str) -> list:
        return [t for t in self.tasks.values()
                if t.type == type_ and t.status is TaskStatus.QUEUED]


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete, enqueue=q.enqueue)


def _pass(conn, task, result, s, **kw):
    return VerifyResult(passed=True, reason="ok")


def _fail(conn, task, result, s, **kw):
    return VerifyResult(passed=False, reason="forced fail")


def test_on_risk_triggers_review_on_failed(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_fail, max_attempts=1, review_mode="on_risk",
                 retro_mode="off", **_seams(q))
    assert r.outcome == "failed"
    assert EVENT_REVIEW_TRIGGERED in sink.types()
    assert len(q.queued_of_type(REVIEW_TASK_TYPE)) == 1


def test_on_risk_does_not_trigger_review_on_clean_done(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_pass, review_mode="on_risk", retro_mode="off", **_seams(q))
    assert r.outcome == "done"
    assert EVENT_REVIEW_TRIGGERED not in sink.types()
    assert q.queued_of_type(REVIEW_TASK_TYPE) == []


def test_always_triggers_review_on_clean_done(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_pass, review_mode="always", retro_mode="off", **_seams(q))
    assert r.outcome == "done"
    assert len(q.queued_of_type(REVIEW_TASK_TYPE)) == 1


def test_off_never_triggers_review(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
             run_verify=_fail, max_attempts=1, review_mode="off", retro_mode="off", **_seams(q))
    assert q.queued_of_type(REVIEW_TASK_TYPE) == []


def test_review_trigger_event_carries_no_marker_or_path(tmp_path):
    """The review.triggered event carries ids/outcome/mode only — no marker/path."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "SECRET-MARK-xyz", "attempt": 1})
    run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
             run_verify=_fail, max_attempts=1, review_mode="always", retro_mode="off", **_seams(q))
    trig = [e for e in sink.events if e.type == EVENT_REVIEW_TRIGGERED]
    assert len(trig) == 1
    assert set(trig[0].payload) == {"review_task_id", "outcome", "mode"}
    assert "SECRET-MARK-xyz" not in str(trig[0].payload)


def test_no_review_loop_a_review_task_enqueues_nothing(tmp_path):
    """Dispatching a review task must NOT enqueue another task (no review-of-review,
    and no retro triggered from a review)."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type=REVIEW_TASK_TYPE,
              payload={"target_task_id": str(uuid4()), "target_task_type": "work.demo",
                       "outcome": "failed"})
    before = len(q.tasks)

    def fake_run_review(conn, task, s, **kw):
        return ReviewResult(ok=False, severity=SEVERITY_HIGH, reasons=["x"])

    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_review=fake_run_review, review_mode="always", retro_mode="always",
                 **_seams(q))
    assert r is not None and r.kind == "review" and r.outcome == "done"
    assert len(q.tasks) == before  # the review enqueued nothing
    assert EVENT_REVIEW_TRIGGERED not in sink.types()
    assert q.queued_of_type(REVIEW_TASK_TYPE) == []


# ===========================================================================
# Live DB — run_review reads the real trail + artifact; evidence beats claims
# ===========================================================================

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _fs_registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    return reg


def _review_task(ws: str, target_id, *, artifact_path=None, marker="",
                 outcome="done", spent_tokens=0, budget_tokens=None, retries=0) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type=REVIEW_TASK_TYPE,
                status=TaskStatus.IN_PROGRESS, priority=0, created_at=now, updated_at=now,
                payload={"target_task_id": str(target_id), "target_task_type": "work.demo",
                         "outcome": outcome, "artifact_path": artifact_path, "marker": marker,
                         "spent_tokens": spent_tokens, "budget_tokens": budget_tokens,
                         "retries": retries})


@pytestmark_db
def test_review_passes_clean_episode(conn, tmp_path):
    ws = f"rev-{uuid4().hex[:12]}"
    target_id = uuid4()
    marker = f"studio-ok:{uuid4().hex[:6]}"
    # A clean episode: the trail claims success AND the real artifact backs it.
    for typ in ("executor.acted", "verify.passed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                       payload={"status": "done"}))
    (tmp_path / "art.txt").write_text(f"{marker}\nall done\n")

    sink = MemoryEventSink()
    result = run_review(
        conn,
        _review_task(ws, target_id, artifact_path="art.txt", marker=marker,
                     spent_tokens=100, budget_tokens=1000),
        sink, registry=_fs_registry(tmp_path), config=load_policy(),
    )
    assert result.ok and result.severity == SEVERITY_NONE
    assert EVENT_REVIEW_PASSED in sink.types()
    assert EVENT_REVIEW_FLAGGED not in sink.types()


@pytestmark_db
def test_review_evidence_beats_lying_model_flags_hallucination(conn, tmp_path, monkeypatch):
    """THE evidence-beats-claim test. The trail CLAIMS done+verified and the model
    is monkeypatched to loudly say 'looks fine' — but the REAL artifact lacks the
    success marker. The Reviewer trusts the observed artifact, not the claim → it
    FLAGS a HIGH-severity hallucinated-success, and (high) escalates with a 🚨
    review.alarm + a real 🛑 approval row."""
    from runtime.approvals import pending_approvals

    ws = f"rev-{uuid4().hex[:12]}"
    target_id = uuid4()
    marker = f"studio-ok:{uuid4().hex[:6]}"
    for typ in ("executor.acted", "verify.passed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                       payload={"status": "done"}))
    # The artifact exists but does NOT contain the required marker (claim unbacked).
    (tmp_path / "art.txt").write_text("all good, trust me — done!\n")

    class _SaysFine:
        text = "PASS — everything looks fine and is verified by the author."

    monkeypatch.setattr(reviewer_mod, "call_model", lambda **kw: _SaysFine())

    review = _review_task(ws, target_id, artifact_path="art.txt", marker=marker)
    sink = MemoryEventSink()
    result = run_review(conn, review, sink,
                        registry=_fs_registry(tmp_path), config=load_policy())

    assert not result.ok and result.severity == SEVERITY_HIGH  # facts, not the claim
    assert any("does not back it" in r for r in result.reasons)
    assert EVENT_REVIEW_FLAGGED in sink.types()
    assert EVENT_REVIEW_ALARM in sink.types()
    # A real 🛑 approval row was raised for this review task.
    pend = [a for a in pending_approvals(conn) if a.task_id == review.id]
    assert pend and pend[0].tier == "🛑" and pend[0].role == "reviewer"


@pytestmark_db
def test_review_flags_over_budget_even_when_artifact_ok(conn, tmp_path):
    """A blown token budget is a disaster signal the Verifier's criterion check
    cannot see — the Reviewer flags it even with a perfectly good artifact."""
    ws = f"rev-{uuid4().hex[:12]}"
    target_id = uuid4()
    marker = f"studio-ok:{uuid4().hex[:6]}"
    for typ in ("executor.acted", "verify.passed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                       payload={"status": "done"}))
    (tmp_path / "art.txt").write_text(f"{marker}\ndone\n")

    sink = MemoryEventSink()
    result = run_review(
        conn,
        _review_task(ws, target_id, artifact_path="art.txt", marker=marker,
                     spent_tokens=5000, budget_tokens=1000),
        sink, registry=_fs_registry(tmp_path), config=load_policy(),
    )
    assert not result.ok and result.severity == SEVERITY_HIGH
    assert any("exceeded budget" in r for r in result.reasons)


@pytestmark_db
def test_review_events_carry_no_secret_marker_or_body(conn, tmp_path):
    """review.* events carry reasons + counts + ids only — never the marker value,
    the artifact body, or arg values (invariants 5 & 6)."""
    ws = f"rev-{uuid4().hex[:12]}"
    target_id = uuid4()
    secret_marker = "studio-ok:SUPER-SECRET-9f3a2b"
    secret_body = "TOP-SECRET-ARTIFACT-CONTENTS"
    for typ in ("executor.acted", "verify.passed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                       payload={"status": "done"}))
    # Artifact body present but missing the marker → hallucination flag fires.
    (tmp_path / "art.txt").write_text(secret_body + "\n")

    sink = MemoryEventSink()
    run_review(
        conn,
        _review_task(ws, target_id, artifact_path="art.txt", marker=secret_marker),
        sink, registry=_fs_registry(tmp_path), config=load_policy(),
    )
    review_events = [e for e in sink.events if e.type.startswith("review.")]
    assert review_events
    for e in review_events:
        blob = str(e.payload)
        assert secret_marker not in blob
        assert secret_body not in blob


@pytestmark_db
def test_run_review_enqueues_nothing_live(conn, tmp_path):
    """A live review raises signals but never touches the task queue (no loop)."""
    ws = f"rev-{uuid4().hex[:12]}"
    target_id = uuid4()
    append_event(conn, make_event(workstream=ws, type="verify.passed", task_id=target_id,
                                  payload={"status": "done"}))
    (tmp_path / "art.txt").write_text("ok\n")
    sink = MemoryEventSink()
    run_review(conn, _review_task(ws, target_id, artifact_path="art.txt", marker="",
                                  outcome="done"),
               sink, registry=_fs_registry(tmp_path), config=load_policy())
    # No task.created event was emitted by the review (it enqueues nothing).
    assert "task.created" not in sink.types()
