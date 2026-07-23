"""Critic role + PM↔Critic consensus + retro consult tests (ADR-0019).

The Critic is the FORWARD-looking adversarial partner on decisions — distinct from
the after-the-fact Reviewer. These tests prove:

- it produces STRUCTURED concerns (risk/downside/missed_opportunity/alternative) +
  a recommendation, computed from FACTS (a lying model does not change the verdict);
- ``critic.reviewed`` carries counts/kinds/severities only — never a body;
- the PM↔Critic consensus loop is BOUNDED: a blocking critique makes the PM revise
  (bounded) then proceed, or escalate a genuine disagreement (🛑) — never looping;
- everything is behavior-preserving when no critic is wired (opt-in).

Pure + fake-queue tests need NO database; the live-DB tests exercise the loop
end-to-end and SKIP cleanly when no Postgres is reachable. Keyless throughout.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.enforce import MemoryEventSink
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.roles.critic import (
    CRITIC_ESCALATE,
    CRITIC_PROCEED,
    CRITIC_REVISE,
    KIND_ALTERNATIVE,
    KIND_MISSED_OPPORTUNITY,
    KIND_RISK,
    SEVERITY_HIGH,
    Concern,
    Critique,
    assess_concerns,
    decide,
    run_critic,
)
from runtime.roles.pm import CONSENSUS_AGREED, CONSENSUS_ESCALATED, run_pm_tick
from runtime.roles.retro import RETRO_TASK_TYPE, run_retro

EVENT_CRITIC_REVIEWED = "critic.reviewed"
EVENT_PM_CONSENSUS = "pm.consensus"


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.delenv("PM_CRITIC_ROUNDS", raising=False)

    def boom(*a, **k):  # pragma: no cover - only on a real network call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _task(type_: str, payload: dict | None = None, workstream: str = "test") -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=workstream, type=type_,
                status=TaskStatus.IN_PROGRESS, priority=0, payload=payload or {},
                created_at=now, updated_at=now)


def _plan_completion(plan: dict):
    return type("C", (), {"text": json.dumps(plan)})()


def _collecting_enqueue(bucket: list):
    def fake_enqueue(conn, *, workstream, type, payload=None, priority=0, **kw) -> Task:
        t = _task(type, payload, workstream).model_copy(update={"status": TaskStatus.UP_FOR_GRABS})
        bucket.append(t)
        return t
    return fake_enqueue


# ===========================================================================
# Pure logic — fact-based concern assessment + verdict (no DB, no model)
# ===========================================================================


def test_assess_plan_flags_missing_criteria_as_blocking_risk():
    # A plan with no success criteria + items lacking their own criterion → HIGH risk.
    concerns = assess_concerns("plan", {
        "n_items": 2, "n_success_criteria": 0, "items_missing_criterion": 2,
        "items_missing_marker": 2, "confidence": 0.9, "has_dependencies": False,
    })
    kinds = {c.kind for c in concerns}
    assert KIND_RISK in kinds
    assert any(c.severity == SEVERITY_HIGH for c in concerns)
    blocking, rec = decide(concerns)
    assert blocking and rec == CRITIC_REVISE


def test_assess_plan_healthy_plan_proceeds():
    # A healthy plan (criteria present, markers present) has only low-severity notes.
    concerns = assess_concerns("plan", {
        "n_items": 2, "n_success_criteria": 1, "items_missing_criterion": 0,
        "items_missing_marker": 0, "confidence": 0.9, "has_dependencies": False,
    })
    blocking, rec = decide(concerns)
    assert not blocking and rec == CRITIC_PROCEED


def test_assess_plan_large_scope_suggests_phasing_alternative():
    concerns = assess_concerns("plan", {
        "n_items": 6, "n_success_criteria": 3, "items_missing_criterion": 0,
        "items_missing_marker": 0, "confidence": 0.7, "has_dependencies": True,
    })
    assert any(c.kind == KIND_ALTERNATIVE for c in concerns)


def test_assess_lessons_flags_failed_without_prevention():
    concerns = assess_concerns("lessons", {
        "n_lessons": 1, "outcome": "failed", "has_prevention_lesson": False,
    })
    assert any(c.kind == KIND_MISSED_OPPORTUNITY for c in concerns)


def test_assess_unknown_subject_has_no_fact_basis():
    assert assess_concerns("whatever", {"anything": 1}) == []


def test_empty_concerns_proceed():
    assert decide([]) == (False, CRITIC_PROCEED)


# ===========================================================================
# run_critic — structured verdict, evidence over a lying model, no leakage
# ===========================================================================


def test_run_critic_returns_structured_critique_and_emits_event():
    sink = MemoryEventSink()
    critique = run_critic(
        "plan for X",
        {"kind": "plan", "n_items": 2, "n_success_criteria": 0,
         "items_missing_criterion": 2, "confidence": 0.9},
        sink=sink,
    )
    assert isinstance(critique, Critique)
    assert critique.blocking and critique.recommendation == CRITIC_REVISE
    assert critique.concerns and all(c.kind in {
        "risk", "downside", "missed_opportunity", "alternative"} for c in critique.concerns)
    # The traceability model call ran and the verdict event was emitted.
    types = sink.types()
    assert "model.routed" in types and "model.call" in types
    assert EVENT_CRITIC_REVIEWED in types


def test_run_critic_verdict_rests_on_facts_not_a_lying_model():
    """A model that loudly says 'looks great' does not flip a fact-based block."""
    sink = MemoryEventSink()

    class _SaysGreat:
        text = "This plan is excellent, ship it — no concerns at all!"

    critique = run_critic(
        "plan", {"kind": "plan", "n_items": 2, "n_success_criteria": 0,
                 "items_missing_criterion": 2, "confidence": 0.9},
        sink=sink, call_model=lambda **kw: _SaysGreat(),
    )
    assert critique.blocking  # evidence (missing criteria) beats the model's praise


def test_critic_reviewed_event_carries_counts_only_no_bodies():
    sink = MemoryEventSink()
    run_critic(
        "plan", {"kind": "plan", "n_items": 2, "n_success_criteria": 0,
                 "items_missing_criterion": 2, "confidence": 0.9},
        sink=sink,
    )
    ev = [e for e in sink.events if e.type == EVENT_CRITIC_REVIEWED][0]
    assert set(ev.payload) == {
        "subject_kind", "concern_count", "kinds", "severities",
        "blocking", "recommendation",
    }
    # No statement / rationale prose leaks onto the event.
    blob = json.dumps(ev.payload)
    assert "criterion" not in blob and "rationale" not in blob and "statement" not in blob
    assert ev.payload["concern_count"] >= 1


# ===========================================================================
# PM↔Critic consensus loop — bounded revise→proceed / escalate, no loop
# ===========================================================================


def _gap_plan() -> dict:
    """A feasible, confident plan with fixable gaps (no criteria/markers)."""
    return {
        "restated_goal": "Build X", "confidence": 0.9, "feasible": True,
        "success_criteria": [],
        "work_items": [
            {"title": "P1", "type": "work.task", "instructions": "do p1"},
            {"title": "P2", "type": "work.task", "instructions": "do p2"},
        ],
    }


def test_pm_revises_on_blocking_critique_then_proceeds():
    """A real critic blocks the gap plan (round 1) → PM revises → proceeds (round 2)."""
    sink = MemoryEventSink()
    enqueued: list = []
    plan = run_pm_tick(
        None, _task("pm.tick", {"goal": "Build X"}), sink,
        enqueue=_collecting_enqueue(enqueued),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=run_critic, critic_rounds=2,
    )
    assert plan.decision == "planned" and plan.work_item_count == 2
    assert len(enqueued) == 2
    # Each enqueued item got a filled criterion + marker from the revision.
    for t in enqueued:
        assert t.payload["criterion"] and t.payload["marker"]
    types = sink.types()
    assert EVENT_PM_CONSENSUS in types and "pm.planned" in types
    assert "pm.pushback" not in types
    consensus = [e for e in sink.events if e.type == EVENT_PM_CONSENSUS][0]
    assert consensus.payload["outcome"] == CONSENSUS_AGREED
    # Two rounds → two critic consults (round 1 blocked, round 2 proceeded).
    assert consensus.payload["rounds"] == 2
    assert sum(1 for e in sink.events if e.type == EVENT_CRITIC_REVIEWED) == 2


def test_pm_escalates_unresolved_disagreement_and_enqueues_no_work():
    """A critic that stays blocking through the bound → 🛑 escalation, no work."""
    sink = MemoryEventSink()
    enqueued: list = []
    approvals: list = []
    calls = {"n": 0}

    def stubborn_critic(subject, context=None, **kw):
        calls["n"] += 1
        return Critique(
            subject_kind="plan", blocking=True, recommendation=CRITIC_REVISE,
            concerns=[Concern(kind=KIND_RISK, severity=SEVERITY_HIGH,
                              statement="fundamental objection", code="x")],
        )

    def fake_request_approval(conn, *, task_id, role, tool, capabilities, tier,
                              reason, sink, workstream, **kw):
        approvals.append({"tier": tier, "role": role, "tool": tool})
        return type("A", (), {"id": uuid4()})()

    plan = run_pm_tick(
        None, _task("pm.tick", {"goal": "Build X"}), sink,
        enqueue=_collecting_enqueue(enqueued),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=stubborn_critic, critic_rounds=2,
        request_approval=fake_request_approval,
    )
    assert plan.decision == "pushback" and plan.approval_id
    assert enqueued == []  # never commits on unresolved disagreement
    assert approvals and approvals[0]["tier"] == "🛑" and approvals[0]["role"] == "pm"
    types = sink.types()
    assert "pm.pushback" in types and "pm.planned" not in types
    consensus = [e for e in sink.events if e.type == EVENT_PM_CONSENSUS][0]
    assert consensus.payload["outcome"] == CONSENSUS_ESCALATED
    # BOUNDED: exactly `critic_rounds` consults — no infinite loop.
    assert calls["n"] == 2


def test_pm_escalates_immediately_on_explicit_escalate_recommendation():
    sink = MemoryEventSink()
    enqueued: list = []
    calls = {"n": 0}

    def escalating_critic(subject, context=None, **kw):
        calls["n"] += 1
        return Critique(subject_kind="plan", blocking=True,
                        recommendation=CRITIC_ESCALATE,
                        concerns=[Concern(kind=KIND_RISK, severity=SEVERITY_HIGH)])

    plan = run_pm_tick(
        None, _task("pm.tick", {"goal": "g"}), sink,
        enqueue=_collecting_enqueue(enqueued),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=escalating_critic, critic_rounds=3,
        request_approval=lambda *a, **k: type("A", (), {"id": uuid4()})(),
    )
    assert plan.decision == "pushback" and enqueued == []
    assert calls["n"] == 1  # escalate is honored on the first consult


def test_pm_bound_of_one_round_blocks_without_revision():
    """With rounds=1 a blocking critique escalates on the single consult (no revise)."""
    sink = MemoryEventSink()
    enqueued: list = []
    calls = {"n": 0}

    def blocker(subject, context=None, **kw):
        calls["n"] += 1
        return Critique(subject_kind="plan", blocking=True,
                        recommendation=CRITIC_REVISE,
                        concerns=[Concern(kind=KIND_RISK, severity=SEVERITY_HIGH)])

    plan = run_pm_tick(
        None, _task("pm.tick", {"goal": "g"}), sink,
        enqueue=_collecting_enqueue(enqueued),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=blocker, critic_rounds=1,
        request_approval=lambda *a, **k: type("A", (), {"id": uuid4()})(),
    )
    assert plan.decision == "pushback" and calls["n"] == 1


def test_pm_behavior_preserving_without_critic():
    """No critic wired → no consensus/critic events; the PM plans exactly as before."""
    sink = MemoryEventSink()
    enqueued: list = []
    plan = run_pm_tick(
        None, _task("pm.tick", {"goal": "Build X"}), sink,
        enqueue=_collecting_enqueue(enqueued),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
    )
    assert plan.decision == "planned" and len(enqueued) == 2
    types = sink.types()
    assert EVENT_PM_CONSENSUS not in types and EVENT_CRITIC_REVIEWED not in types


def test_pm_consensus_event_carries_no_plan_body():
    sink = MemoryEventSink()
    run_pm_tick(
        None, _task("pm.tick", {"goal": "Build X"}), sink,
        enqueue=_collecting_enqueue([]),
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=run_critic, critic_rounds=2,
    )
    ev = [e for e in sink.events if e.type == EVENT_PM_CONSENSUS][0]
    assert set(ev.payload) == {"goal", "rounds", "outcome", "concern_count"}
    assert "work_items" not in json.dumps(ev.payload)
    assert "criterion" not in json.dumps(ev.payload)


# ===========================================================================
# Retro consult (opt-in) — challenge the lessons, behavior-preserving default
# ===========================================================================


def _retro_task(ws: str = "t") -> Task:
    return _task(RETRO_TASK_TYPE, {"target_task_id": str(uuid4()),
                                   "target_task_type": "work.demo",
                                   "outcome": "failed"}, ws)


def _fake_trail(*types_):
    def read(conn, **kw):
        return [make_event(workstream="t", type=t) for t in types_]
    return read


def test_retro_consults_critic_records_recommendation_and_emits_event():
    sink = MemoryEventSink()
    res = run_retro(
        None, _retro_task(), sink, critic=run_critic,
        add_lesson=lambda conn, ws, text, metadata=None: type("L", (), {"id": uuid4()})(),
        read=_fake_trail("executor.acted", "verify.failed", "task.finished"),
    )
    assert res.critic_recommendation in (CRITIC_PROCEED, CRITIC_REVISE)
    assert EVENT_CRITIC_REVIEWED in sink.types()
    # The lessons were still distilled + stored (advisory consult, not a gate).
    assert res.lessons_count >= 1


def test_retro_behavior_preserving_without_critic():
    sink = MemoryEventSink()
    res = run_retro(
        None, _retro_task(), sink,
        add_lesson=lambda conn, ws, text, metadata=None: type("L", (), {"id": uuid4()})(),
        read=_fake_trail("executor.acted", "verify.failed", "task.finished"),
    )
    assert res.critic_recommendation is None
    assert EVENT_CRITIC_REVIEWED not in sink.types()


# ===========================================================================
# Live DB — the loop end-to-end (skips cleanly with no Postgres)
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


@pytestmark_db
def test_run_critic_live_emits_reviewed_event(conn):
    from runtime.enforce import DbEventSink
    from runtime.events import read_events

    ws = f"critic-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    task_id = uuid4()
    critique = run_critic(
        "plan", {"kind": "plan", "n_items": 2, "n_success_criteria": 0,
                 "items_missing_criterion": 2, "confidence": 0.9},
        sink=sink, conn=conn, task_id=task_id, workstream=ws,
    )
    assert critique.blocking
    trail = [e.type for e in read_events(conn, workstream=ws)]
    assert EVENT_CRITIC_REVIEWED in trail


@pytestmark_db
def test_pm_consensus_live_revises_then_decomposes(conn):
    from runtime.enforce import DbEventSink
    from runtime.events import read_events
    from runtime.tasks import enqueue_task

    ws = f"consensus-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Build the live thing"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=run_critic, critic_rounds=2,
    )
    assert result.decision == "planned" and result.work_item_count == 2
    trail = [e.type for e in read_events(conn, workstream=ws)]
    assert EVENT_PM_CONSENSUS in trail and EVENT_CRITIC_REVIEWED in trail
    # The decomposed work tasks actually landed on the board.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
                    (ws,))
        n = int(cur.fetchone()["n"])
    if not conn.autocommit:
        conn.commit()
    assert n == 2
