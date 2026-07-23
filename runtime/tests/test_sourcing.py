"""Sourcing role tests (ADR-0005 / ADR-0006 / ADR-0014).

Pure-logic tests (candidate synthesis grounded in search evidence, the approval-
envelope classification, YAML rendering) and worker-wiring tests (sourcing dispatch,
NO loop) run with NO database via an in-memory fake queue. The live-DB tests
exercise ``run_sourcing`` end-to-end:

- research goes through the policy-gated cached gateway (``net.fetch``); a role
  without that capability is DENIED (no provider call, no candidate write);
- an in-band swap produces a reviewable candidate + auto-adopts + emits 📣
  ``sourcing.autoadopted``;
- a new-provider / budget-increasing change raises a real 🛑 approval and does NOT
  auto-adopt;
- the candidate is written to a review path via the policy-gated filesystem tool
  (denied cleanly without ``fs.write``);
- ``sourcing.*`` events leak no secret/API key and no raw provenance URL (only the
  hash), and the live registry is never mutated;
- a sourcing task enqueues nothing (no loop).

They SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
import yaml

from runtime import db
from runtime.approvals import EVENT_APPROVAL_REQUESTED, pending_approvals
from runtime.capabilities import Capability
from runtime.enforce import MemoryEventSink
from runtime.migrate import migrate
from runtime.model.registry import ModelSpec, Registry, Tier, load_registry
from runtime.models import Task, TaskStatus
from runtime.policy import PolicyConfig
from runtime.roles.sourcing import (
    DECISION_APPROVAL,
    DECISION_AUTOADOPT,
    DEFAULT_RESULTS,
    EVENT_SOURCING_AUTOADOPTED,
    EVENT_SOURCING_PROPOSED,
    SOURCING_TASK_TYPES,
    SourcingResult,
    classify_candidate,
    provenance_hash,
    render_candidate_yaml,
    run_sourcing,
    synthesize_candidate,
    CandidateDecision,
)
from runtime.search import (
    EVENT_SEARCH_CACHE_HIT,
    EVENT_SEARCH_CACHE_MISS,
    EVENT_SEARCH_DENIED,
    EVENT_SEARCH_PROVIDER_CALL,
    SearchDenied,
    SearchResult,
)
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import run_once

# Policies used across the tests — least privilege per scenario.
NET_AND_WRITE = PolicyConfig(
    roles={"sourcing": frozenset({Capability.NET_FETCH, Capability.FS_WRITE})}
)
NET_ONLY = PolicyConfig(roles={"sourcing": frozenset({Capability.NET_FETCH})})
READ_ONLY = PolicyConfig(roles={"sourcing": frozenset({Capability.FS_READ})})


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    # A fake key present in the env must NEVER reach an event (invariants 5 & 6).
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(env, f"sk-fake-{env.lower()}-SECRET")
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.setenv("SEARCH_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover - a network call would be a bug
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _results(n: int) -> list[SearchResult]:
    return [
        SearchResult(title=f"LMArena {i}", url=f"https://example.invalid/src{i}", score=1.0 - i / 10)
        for i in range(n)
    ]


def _base_registry() -> Registry:
    """A small, deterministic registry to classify against (no file dependency)."""
    return Registry.from_dict(
        {
            "models": [
                {"id": "claude-sonnet-5", "provider": "anthropic", "tier": "mid",
                 "price_in": 3.0, "price_out": 15.0, "context_window": 1000000},
                {"id": "claude-haiku-4.5", "provider": "anthropic", "tier": "cheap",
                 "price_in": 1.0, "price_out": 5.0, "context_window": 200000},
            ],
            "routing": {},
        }
    )


# ===========================================================================
# Pure logic — synthesis, classification, rendering (no DB, no model, no net)
# ===========================================================================


def test_synthesize_grounds_provenance_in_search_not_a_bare_claim():
    # A bogus provenance claim in the candidate is IGNORED; provenance comes from
    # the search results the gateway actually returned (evidence over claims).
    cand = {"id": "x", "provider": "anthropic", "tier": "mid",
            "price_in": 1.0, "price_out": 2.0, "context_window": 1000,
            "provenance": "trust me bro"}
    spec = synthesize_candidate(cand, _results(3), sourced_on="2026-07-22")
    assert "example.invalid/src0" in spec.provenance
    assert "trust me bro" not in spec.provenance
    assert spec.provenance_date == "2026-07-22"


def test_synthesize_with_no_sources_notes_missing_evidence():
    cand = {"id": "x", "provider": "anthropic", "tier": "mid", "price_in": 1.0,
            "price_out": 2.0, "context_window": 1000}
    spec = synthesize_candidate(cand, [], sourced_on="2026-07-22")
    assert "no corroborating source" in spec.provenance


def test_provenance_hash_is_stable_and_order_independent():
    a = ModelSpec(id="a", provider="p", tier=Tier.MID, price_in=1, price_out=2,
                  context_window=1, provenance="u1")
    b = ModelSpec(id="b", provider="p", tier=Tier.MID, price_in=1, price_out=2,
                  context_window=1, provenance="u2")
    assert provenance_hash([a, b]) == provenance_hash([b, a])
    assert provenance_hash([a, b]) != provenance_hash([a])


def test_classify_in_band_swap_autoadopts():
    reg = _base_registry()
    # Same known model, LOWER price → an in-band swap within the cost/quality band.
    spec = ModelSpec(id="claude-sonnet-5", provider="anthropic", tier=Tier.MID,
                     price_in=2.5, price_out=14.0, context_window=1000000)
    decision, reason = classify_candidate(spec, reg)
    assert decision == DECISION_AUTOADOPT
    assert "in-band" in reason


def test_classify_new_provider_requires_approval():
    reg = _base_registry()
    spec = ModelSpec(id="acme-1", provider="acme", tier=Tier.MID,
                     price_in=0.1, price_out=0.2, context_window=1000)
    decision, reason = classify_candidate(spec, reg)
    assert decision == DECISION_APPROVAL
    assert "new provider" in reason


def test_classify_budget_increasing_requires_approval():
    reg = _base_registry()
    # Known provider + model, but a HIGHER output price → budget-increasing.
    spec = ModelSpec(id="claude-sonnet-5", provider="anthropic", tier=Tier.MID,
                     price_in=3.0, price_out=20.0, context_window=1000000)
    decision, reason = classify_candidate(spec, reg)
    assert decision == DECISION_APPROVAL
    assert "budget-increasing" in reason


def test_classify_new_tier_requires_approval():
    reg = _base_registry()  # has no 'pm' tier
    spec = ModelSpec(id="new-pm", provider="anthropic", tier=Tier.PM,
                     price_in=5.0, price_out=25.0, context_window=1000000)
    decision, reason = classify_candidate(spec, reg)
    assert decision == DECISION_APPROVAL
    assert "new tier" in reason


def test_classify_scope_affecting_flag_forces_approval():
    reg = _base_registry()
    spec = ModelSpec(id="claude-sonnet-5", provider="anthropic", tier=Tier.MID,
                     price_in=1.0, price_out=1.0, context_window=1000000)  # cheaper
    decision, reason = classify_candidate(spec, reg, scope_affecting=True)
    assert decision == DECISION_APPROVAL
    assert "scope-affecting" in reason


def test_render_candidate_yaml_is_parseable_and_carries_specs():
    spec = ModelSpec(id="m1", provider="anthropic", tier=Tier.MID, price_in=1,
                     price_out=2, context_window=10, provenance="u")
    d = CandidateDecision(spec=spec, decision=DECISION_AUTOADOPT, reason="ok")
    text = render_candidate_yaml([d], proposal_decision=DECISION_AUTOADOPT,
                                 prov_hash="abc123", sourced_on="2026-07-22")
    assert "CANDIDATE" in text and "abc123" in text
    doc = yaml.safe_load(text)
    assert doc["models"][0]["id"] == "m1"
    assert doc["models"][0]["_decision"] == DECISION_AUTOADOPT


# ===========================================================================
# Worker wiring — sourcing dispatch + NO loop (fake queue, no DB)
# ===========================================================================


class FakeQueue:
    """Minimal in-memory queue mirroring the real enqueue/claim/heartbeat/complete."""

    def __init__(self) -> None:
        self.tasks: dict = {}
        self.order: list = []

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type, status=TaskStatus.UP_FOR_GRABS,
                 priority=priority, payload=payload or {}, created_at=now, updated_at=now)
        self.tasks[t.id] = t
        self.order.append(t.id)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order if self.tasks[i].status is TaskStatus.UP_FOR_GRABS
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        t = self.tasks[ids[0]].model_copy(update={
            "status": TaskStatus.IN_PROGRESS, "claimed_by": worker_id,
            "heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[t.id] = t
        return t

    def heartbeat(self, conn, task_id, worker_id):
        return self.tasks.get(task_id)

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.MERGED,
                 spent_tokens=None, force=False):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"status": status, "result": result})
        self.tasks[task_id] = t
        return t


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete, enqueue=q.enqueue)


@pytest.mark.parametrize("task_type", list(SOURCING_TASK_TYPES))
def test_sourcing_task_dispatches_and_enqueues_nothing(tmp_path, task_type):
    """A sourcing task is handled by run_sourcing and NEVER enqueues (no loop)."""
    sink = MemoryEventSink()
    q = FakeQueue()
    q.enqueue(None, workstream="t", type=task_type, payload={})
    before = len(q.tasks)

    def fake_run_sourcing(conn, task, s, **kw):
        # A sourcing task must never receive an `enqueue` seam from the worker.
        assert "enqueue" not in kw
        return SourcingResult(candidate_count=1, model_ids=["m1"], provenance_hash="h",
                              decision=DECISION_AUTOADOPT, autoadopted=True)

    r = run_once(None, "w1", sink, registry=_registry(tmp_path),
                 run_sourcing=fake_run_sourcing, **_seams(q))
    assert r is not None and r.kind == "sourcing" and r.outcome == "done"
    assert len(q.tasks) == before  # no new task enqueued


# ===========================================================================
# Live DB — gateway path, candidate, envelope, no leak, no loop
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


def _sourcing_task(ws: str, **payload) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type=SOURCING_TASK_TYPES[0],
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload=payload)


def _in_band_candidate() -> dict:
    reg = load_registry()
    ref = reg.get("claude-sonnet-5") or next(iter(reg.models.values()))
    return {"id": ref.id, "provider": ref.provider, "tier": ref.tier.value,
            "price_in": max(0.0, ref.price_in - 0.5), "price_out": max(0.0, ref.price_out - 1.0),
            "context_window": ref.context_window, "task_fit": list(ref.task_fit)}


@pytestmark_db
def test_sourcing_in_band_swap_autoadopts_and_writes_candidate(conn, tmp_path):
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    result = run_sourcing(
        conn, _sourcing_task(ws, candidates=[_in_band_candidate()]), sink,
        tool_registry=reg, policy=NET_AND_WRITE,
    )
    assert isinstance(result, SourcingResult)
    assert result.decision == DECISION_AUTOADOPT and result.autoadopted is True
    # Research went through the policy-gated cached gateway (cache hit OR miss →
    # provider), NEVER agent-direct. The cache persists across runs, so either the
    # cold-miss (miss + provider_call) or the warm-hit path proves the gateway ran.
    assert EVENT_SEARCH_CACHE_MISS in sink.types() or EVENT_SEARCH_CACHE_HIT in sink.types()
    if EVENT_SEARCH_CACHE_MISS in sink.types():
        assert EVENT_SEARCH_PROVIDER_CALL in sink.types()
    # 📣 auto-adopted (in-band); NO 🛑 approval raised.
    assert EVENT_SOURCING_AUTOADOPTED in sink.types()
    assert result.approval_id is None
    # The reviewable candidate was written to the review path (not the live registry).
    assert result.candidate_status == "executed"
    cand_file = tmp_path / result.candidate_path
    assert cand_file.exists()
    doc = yaml.safe_load(cand_file.read_text())
    assert doc["models"] and doc["models"][0]["_decision"] == DECISION_AUTOADOPT


@pytestmark_db
def test_sourcing_new_provider_raises_stop_approval(conn, tmp_path):
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    new_prov = {"id": "acme-flash-1", "provider": "acme", "tier": "mid",
                "price_in": 0.1, "price_out": 0.2, "context_window": 128000}
    result = run_sourcing(
        conn, _sourcing_task(ws, candidates=[new_prov]), sink,
        tool_registry=reg, policy=NET_AND_WRITE,
    )
    assert result.decision == DECISION_APPROVAL
    assert result.autoadopted is False
    assert "acme" in result.new_providers
    # A REAL 🛑 approval row was created + the approval.requested event emitted.
    assert result.approval_id is not None
    assert EVENT_APPROVAL_REQUESTED in sink.types()
    assert EVENT_SOURCING_AUTOADOPTED not in sink.types()
    assert any(str(a.id) == result.approval_id for a in pending_approvals(conn))
    # The proposal event records the approval decision (still a reviewable candidate).
    proposed = [e for e in sink.events if e.type == EVENT_SOURCING_PROPOSED]
    assert proposed and proposed[0].payload["decision"] == DECISION_APPROVAL


@pytestmark_db
def test_sourcing_budget_increase_raises_stop_approval(conn, tmp_path):
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    refc = _in_band_candidate()
    refc = {**refc, "price_out": refc["price_out"] + 100.0}  # budget-increasing
    result = run_sourcing(
        conn, _sourcing_task(ws, candidates=[refc]), sink,
        tool_registry=reg, policy=NET_AND_WRITE,
    )
    assert result.decision == DECISION_APPROVAL
    assert result.budget_increasing_ids == [refc["id"]]
    assert result.approval_id is not None


@pytestmark_db
def test_sourcing_search_denied_without_net_fetch(conn, tmp_path):
    """A role lacking net.fetch is denied by the gateway — nothing fetched/written."""
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    with pytest.raises(SearchDenied):
        run_sourcing(conn, _sourcing_task(ws, candidates=[_in_band_candidate()]),
                     sink, tool_registry=reg, policy=READ_ONLY)
    assert EVENT_SEARCH_DENIED in sink.types()
    assert EVENT_SEARCH_PROVIDER_CALL not in sink.types()
    # No candidate written and no proposal emitted (we never got past the gateway).
    assert EVENT_SOURCING_PROPOSED not in sink.types()
    assert not any(tmp_path.rglob("*.candidate.yaml"))


@pytestmark_db
def test_sourcing_candidate_write_denied_without_fs_write(conn, tmp_path):
    """Writing the candidate is policy-gated: no fs.write ⇒ no file, but classify runs."""
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    result = run_sourcing(
        conn, _sourcing_task(ws, candidates=[_in_band_candidate()]), sink,
        tool_registry=reg, policy=NET_ONLY,  # net.fetch but NO fs.write
    )
    assert result.candidate_status == "denied"
    assert result.candidate_path is None
    assert not any(tmp_path.rglob("*.candidate.yaml"))
    # The envelope decision still resolved (in-band → auto-adopt 📣).
    assert result.decision == DECISION_AUTOADOPT
    assert EVENT_SOURCING_PROPOSED in sink.types()


@pytestmark_db
def test_sourcing_events_leak_no_secret_or_url(conn, tmp_path):
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    run_sourcing(conn, _sourcing_task(ws, candidates=[_in_band_candidate()]),
                 sink, tool_registry=reg, policy=NET_AND_WRITE)
    blob = " ".join(str(e.payload) for e in sink.events)
    # No API key from the env ever reaches an event.
    assert "SECRET" not in blob
    # Provenance is emitted as a HASH — the raw source URL never appears in events.
    assert "example.invalid" not in blob
    # The sourcing.proposed payload carries exactly the safe fields.
    proposed = [e for e in sink.events if e.type == EVENT_SOURCING_PROPOSED][0]
    assert set(proposed.payload) == {
        "model_ids", "candidate_count", "provenance_hash", "decision",
        "new_provider_count", "budget_increasing_count", "candidate_written", "autoadopted",
    }


@pytestmark_db
def test_sourcing_never_mutates_live_registry_and_no_loop(conn, tmp_path):
    ws = f"src-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    before = load_registry().model_dump()
    run_sourcing(conn, _sourcing_task(ws, candidates=[_in_band_candidate()]),
                 sink, tool_registry=reg, policy=NET_AND_WRITE)
    # The live registry is untouched — only the candidate (under the tool root) is written.
    assert load_registry().model_dump() == before
    # No follow-on task was spawned (no sourcing-loop).
    assert "task.created" not in sink.types()
