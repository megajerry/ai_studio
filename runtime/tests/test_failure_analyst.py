"""Failure-pattern analyst tests (ADR-0023 R3).

Pure-logic tests (recurrence detection using the CI LOWER bound + sample floor so a
tiny sample never fires; metric round-trip) run with NO database. Live-DB tests
exercise ``run_failure_analysis`` end-to-end:

- a RECURRING ``model.call.failed`` error_type above the threshold (n ≥ floor) is
  detected → a reviewable durable-fix proposal is written via the policy-gated
  filesystem tool → an ``experiment.proposed`` is registered with a target metric →
  body-free ``failure.pattern_detected`` + ``fix.proposed`` events are emitted, and
  NO fix is applied / NO experiment is started / nothing is enqueued (no loop);
- a tiny sample does NOT fire (no proposal, no experiment, no events);
- without ``fs.write`` the proposal write is DENIED cleanly (experiment still framed);
- the verify-as-experiment hook drives ``experiment.evaluated`` from REAL post-fix
  traffic: a dropped rate → effective (kept), an unchanged rate → ineffective (killed).

They SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.enforce import DbEventSink, MemoryEventSink
from runtime.event_types import (
    EVENT_FAILURE_PATTERN_DETECTED,
    EVENT_FIX_PROPOSED,
)
from runtime.events import append_event
from runtime.experiment import (
    ExperimentDecision,
    ExperimentStatus,
    get_experiment,
    start_experiment,
)
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.policy import PolicyConfig
from runtime.quality import _rate_ci
from runtime.roles.failure_analyst import (
    DEFAULT_MIN_SAMPLE,
    KIND_MODEL_CALL_ERROR,
    KIND_TASK_STALL,
    detect_patterns,
    metric_name_for,
    observe_and_evaluate_fix,
    pattern_rate_from_report,
    run_failure_analysis,
)
from runtime.tools import FilesystemTool, ToolRegistry

WRITE = PolicyConfig(roles={"failure_analyst": frozenset(
    {Capability.FS_READ, Capability.FS_WRITE})})
READ_ONLY = PolicyConfig(roles={"failure_analyst": frozenset({Capability.FS_READ})})


# ===========================================================================
# Pure logic — recurrence detection + metric round-trip (no DB)
# ===========================================================================


def _report(error_shape=(), stall_shape=(), total_calls=0, terminal=0) -> dict:
    return {
        "totals": {"model_calls_total": total_calls, "tasks_terminal": terminal},
        "by_error_type": [
            {"error_type": k, "count": c, "share": _rate_ci(c, total_calls)}
            for k, c in error_shape
        ],
        "by_stall_reason": [
            {"stall_reason": k, "count": c, "share": _rate_ci(c, terminal)}
            for k, c in stall_shape
        ],
    }


def test_detects_recurring_error_above_threshold_with_large_n():
    rep = _report(error_shape=[("RateLimitError", 40), ("TimeoutError", 2)],
                  total_calls=100)
    pats = detect_patterns(rep, threshold=0.2, min_sample=30)
    assert len(pats) == 1
    p = pats[0]
    assert p.pattern_id == "model_call_error:RateLimitError"
    assert p.kind == KIND_MODEL_CALL_ERROR and p.key == "RateLimitError"
    assert p.successes == 40 and p.n == 100 and p.rate == 0.4
    # Fired BECAUSE the CI lower bound clears the threshold.
    assert p.ci95[0] > 0.2


def test_tiny_sample_never_fires_even_at_rate_1():
    # 3/3 = a perfect 1.0 point estimate, but n < floor and the CI lower bound is
    # far below the threshold → NOT a recurring pattern (the statistical-rigor fix).
    rep = _report(error_shape=[("RateLimitError", 3)], total_calls=3)
    assert detect_patterns(rep, threshold=0.2, min_sample=DEFAULT_MIN_SAMPLE) == []


def test_large_n_but_low_rate_does_not_fire():
    rep = _report(error_shape=[("Flaky", 5)], total_calls=500)  # 1% — not recurring
    assert detect_patterns(rep, threshold=0.2, min_sample=30) == []


def test_borderline_point_estimate_above_but_ci_lower_below_does_not_fire():
    # 8/30 ≈ 0.267 point estimate > 0.2, but Wilson lower bound < 0.2 on n=30 →
    # honest guard: not enough evidence to call it recurring yet.
    share = _rate_ci(8, 30)
    assert share["rate"] > 0.2 and share["ci95"][0] < 0.2
    rep = _report(error_shape=[("Edge", 8)], total_calls=30)
    assert detect_patterns(rep, threshold=0.2, min_sample=30) == []


def test_detects_recurring_stall_reason():
    rep = _report(stall_shape=[("no_progress", 20)], terminal=40)
    pats = detect_patterns(rep, threshold=0.2, min_sample=30)
    assert len(pats) == 1 and pats[0].kind == KIND_TASK_STALL
    assert pats[0].pattern_id == "task_stall:no_progress"


def test_metric_name_round_trips_through_report():
    name = metric_name_for("model_call_error:RateLimitError")
    assert name == "failure_rate:model_call_error:RateLimitError"
    rep = _report(error_shape=[("RateLimitError", 18)], total_calls=100)
    assert pattern_rate_from_report(rep, name) == 0.18
    # Absent category over real traffic → rate 0.0 (not None); no traffic → None.
    assert pattern_rate_from_report(rep, metric_name_for("model_call_error:Gone")) == 0.0
    empty = _report(total_calls=0)
    assert pattern_rate_from_report(empty, name) is None


def test_pattern_rate_rejects_foreign_metric():
    with pytest.raises(ValueError):
        pattern_rate_from_report(_report(), "cost_usd")


# ===========================================================================
# Live DB — detect → propose candidate + experiment → verify-as-experiment
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


@pytest.fixture
def ws() -> str:
    return f"fa-{uuid4().hex[:12]}"


def _analyst_task(ws: str, **payload) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type="analyze.failures",
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload=payload)


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    return reg


def _seed_api_errors(conn, ws, *, ok, failed, error_type="RateLimitError"):
    for _ in range(ok):
        append_event(conn, make_event(workstream=ws, type="model.call",
                                       payload={"model": "dryrun", "cost_usd": 0.0,
                                                "input_tokens": 1, "output_tokens": 1}))
    for _ in range(failed):
        append_event(conn, make_event(
            workstream=ws, type="model.call.failed",
            payload={"error_type": error_type, "model": "m", "provider": "p",
                     "role": "executor", "task_type": "work"}))
    conn.commit()


def _max_seq(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(seq) AS s FROM events")
        s = cur.fetchone()["s"]
    conn.commit()
    return int(s)


@pytestmark_db
def test_recurring_failure_proposes_fix_and_experiment_no_autoapply(conn, ws, tmp_path):
    _seed_api_errors(conn, ws, ok=60, failed=40)  # 40% RateLimitError over n=100
    sink = MemoryEventSink()

    result = run_failure_analysis(
        conn, _analyst_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
        threshold=0.2, min_sample=30,
    )

    # A recurring pattern was recognized and a fix framed.
    assert result.patterns_detected == 1
    fix = result.proposals[0]
    assert fix.pattern_id == "model_call_error:RateLimitError"
    assert fix.n == 100 and fix.rate == 0.4

    # A REVIEWABLE proposal artifact was written via the policy-gated tool.
    assert fix.proposal_status == "executed"
    assert fix.proposal_path == "proposals/fixes/model_call_error__RateLimitError.md"
    written = Path(tmp_path) / fix.proposal_path
    assert written.exists() and "durable fix" in written.read_text()

    # The fix is registered as an experiment.proposed (NOT started — no fix applied).
    assert fix.experiment_id is not None
    exp = get_experiment(conn, fix.experiment_id)
    assert exp.status is ExperimentStatus.PROPOSED
    assert exp.success_metric.name == fix.metric_name
    assert exp.success_metric.comparator == "<=" and exp.success_metric.target == 0.2

    # Body-free events emitted; a fix was NEVER auto-applied.
    types = sink.types()
    assert types.count(EVENT_FAILURE_PATTERN_DETECTED) == 1
    assert types.count(EVENT_FIX_PROPOSED) == 1
    det = next(e for e in sink.events if e.type == EVENT_FAILURE_PATTERN_DETECTED)
    assert det.payload["error_type"] == "RateLimitError"
    assert det.payload["n"] == 100 and det.payload["rate"] == 0.4
    assert det.payload["ci95"][0] > 0.2  # fired on the CI lower bound
    prop = next(e for e in sink.events if e.type == EVENT_FIX_PROPOSED)
    assert prop.payload["auto_applied"] is False
    assert prop.payload["experiment_id"] == fix.experiment_id
    # No prompt/response/secret body ever travels.
    for e in sink.events:
        blob = str(e.payload)
        assert "prompt" not in blob and "SECRET" not in blob

    # No loop: the analyst enqueued NOTHING for this workstream.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s", (ws,))
        assert int(cur.fetchone()["n"]) == 0
    conn.commit()


@pytestmark_db
def test_tiny_sample_does_not_propose_anything(conn, ws, tmp_path):
    _seed_api_errors(conn, ws, ok=1, failed=3)  # n=4 < floor → not recurring
    sink = MemoryEventSink()
    result = run_failure_analysis(
        conn, _analyst_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE, threshold=0.2, min_sample=30,
    )
    assert result.patterns_detected == 0 and result.proposals == []
    assert sink.types() == []  # nothing detected → nothing emitted


@pytestmark_db
def test_without_fs_write_proposal_is_denied_but_experiment_still_framed(conn, ws, tmp_path):
    _seed_api_errors(conn, ws, ok=60, failed=40)
    sink = MemoryEventSink()
    result = run_failure_analysis(
        conn, _analyst_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=READ_ONLY, threshold=0.2, min_sample=30,
    )
    fix = result.proposals[0]
    assert fix.proposal_status == "denied" and fix.proposal_path is None
    assert not (Path(tmp_path) / "proposals").exists()  # nothing written
    # The framing still happened (experiment proposed, events emitted).
    assert fix.experiment_id is not None
    assert EVENT_FIX_PROPOSED in sink.types()


@pytestmark_db
def test_verify_as_experiment_effective_when_rate_drops(conn, ws, tmp_path):
    # 1. Detect the recurring failure + propose the fix experiment.
    _seed_api_errors(conn, ws, ok=60, failed=40)
    sink = MemoryEventSink()
    result = run_failure_analysis(
        conn, _analyst_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE, threshold=0.2, min_sample=30,
    )
    exp_id = result.proposals[0].experiment_id

    # 2. A human applies the fix + starts observing; capture the post-fix cursor.
    #    The lifecycle uses a DbEventSink so the observation persists to the log the
    #    evidence-based evaluator reads (production always passes DbEventSink).
    dbsink = DbEventSink(conn)
    start_experiment(conn, exp_id, sink=dbsink)  # → running (no work items enqueued)
    cursor = _max_seq(conn)

    # 3. Real POST-FIX traffic: the failure rate dropped to 0.18 (18/100).
    _seed_api_errors(conn, ws, ok=82, failed=18)

    exp = observe_and_evaluate_fix(conn, exp_id, sink=dbsink, workstream=ws, since_seq=cursor)
    assert exp.status is ExperimentStatus.KEPT  # effective: rate <= 0.2 target
    assert exp.decision is ExperimentDecision.KEPT
    assert exp.observed_value == 0.18  # computed from real post-fix events


@pytestmark_db
def test_verify_as_experiment_ineffective_when_rate_persists(conn, ws, tmp_path):
    _seed_api_errors(conn, ws, ok=60, failed=40)
    sink = MemoryEventSink()
    result = run_failure_analysis(
        conn, _analyst_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE, threshold=0.2, min_sample=30,
    )
    exp_id = result.proposals[0].experiment_id
    dbsink = DbEventSink(conn)
    start_experiment(conn, exp_id, sink=dbsink)
    cursor = _max_seq(conn)

    # Post-fix traffic still fails at 0.4 → the fix did NOT work.
    _seed_api_errors(conn, ws, ok=60, failed=40)
    exp = observe_and_evaluate_fix(conn, exp_id, sink=dbsink, workstream=ws, since_seq=cursor)
    assert exp.status is ExperimentStatus.KILLED  # ineffective: rate stayed above target
    assert exp.observed_value == 0.4
