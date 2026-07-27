"""Role-agnostic dispatch registry tests (ADR-0031).

Proves the refactor from the bespoke if/elif chain to the single task_type→handler
registry is behavior-preserving and that the previously-dormant roles now dispatch:

(a) REGRESSION — ``resolve_handler`` maps EVERY existing task type to the SAME
    handler adapter as the old chain, preserving the specific-before-prefix invariant
    (``work.code`` → the loop-free coding path, not the generic work loop); an unknown
    type resolves to ``None`` (the abandoned fallback).
(b) the newly-registered Capacity Steward (``capacity.review``) now dispatches through
    ``run_once`` (was unknown→abandoned before), commits MERGED, heartbeats, and
    enqueues nothing (no loop). The other dormant roles' dispatch is covered in
    test_worker.py (sourcing/failure/curator) and here via the mapping table.
(c) the PM can enqueue a role task (``enqueue_role_task``) and it later dispatches —
    and every PM-commissionable role type actually resolves to a live handler.
(d) an unknown type is still abandoned (also asserted via ``resolve_handler``).

Drives ``run_once`` with the in-memory FakeQueue from test_worker (no DB), fully
keyless — the same idiom the core-loop tests use.
"""

from __future__ import annotations

import httpx
import pytest

from runtime.enforce import MemoryEventSink
from runtime.models import TaskStatus
from runtime.policy import load_policy
from runtime.roles.capacity_steward import (
    CAPACITY_REVIEW_TYPE,
    CapacityFlag,
    CapacityReport,
)
from runtime.roles.curator import CURATOR_TASK_TYPES
from runtime.roles.failure_analyst import FAILURE_ANALYST_TASK_TYPES
from runtime.roles.pm import (
    PM_ROLE_TASK_TYPES,
    REPLAN_TASK_TYPE,
    enqueue_role_task,
    role_catalog_note,
)
from runtime.roles.researcher import RESEARCH_TASK_TYPE
from runtime.roles.reviewer import REVIEW_TASK_TYPE
from runtime.roles.retro import RETRO_TASK_TYPE
from runtime.roles.skill_lifecycle import SKILL_LIFECYCLE_TASK_TYPES
from runtime.roles.sourcing import SOURCING_TASK_TYPES
from runtime.crossworkstream import FEATURE_REQUEST_TYPE
from runtime.scheduler import PM_TICK_TYPE
from runtime.worker import (
    CODE_TASK_TYPES,
    SPOKESMAN_PREP_TYPE,
    _dispatch_capacity,
    _dispatch_code,
    _dispatch_curator,
    _dispatch_failure_analysis,
    _dispatch_feature_request,
    _dispatch_pm_tick,
    _dispatch_replan,
    _dispatch_research,
    _dispatch_retro,
    _dispatch_review,
    _dispatch_skill_lifecycle,
    _dispatch_sourcing,
    _dispatch_spokesman_prep,
    _dispatch_work,
    resolve_handler,
    run_once,
)

# Reuse the fake-queue harness from the core-loop tests (no DB, keyless).
from runtime.tests.test_worker import FakeQueue, _registry, _seams


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


# ===========================================================================
# (a) + (d) REGRESSION: the registry resolves every type to the same handler
# ===========================================================================

# The EXACT expected mapping — the single source of truth the old if/elif chain
# encoded, now asserted against the registry. If a future edit drops or misroutes a
# type, this table fails loudly (the critical safety net).
_EXPECTED = [
    (PM_TICK_TYPE, _dispatch_pm_tick),
    (SPOKESMAN_PREP_TYPE, _dispatch_spokesman_prep),
    (REPLAN_TASK_TYPE, _dispatch_replan),
    (RETRO_TASK_TYPE, _dispatch_retro),
    (RESEARCH_TASK_TYPE, _dispatch_research),
    (REVIEW_TASK_TYPE, _dispatch_review),
    (FEATURE_REQUEST_TYPE, _dispatch_feature_request),
    (CAPACITY_REVIEW_TYPE, _dispatch_capacity),
    *[(t, _dispatch_sourcing) for t in SOURCING_TASK_TYPES],
    *[(t, _dispatch_failure_analysis) for t in FAILURE_ANALYST_TASK_TYPES],
    *[(t, _dispatch_curator) for t in CURATOR_TASK_TYPES],
    *[(t, _dispatch_skill_lifecycle) for t in SKILL_LIFECYCLE_TASK_TYPES],
    *[(t, _dispatch_code) for t in CODE_TASK_TYPES],
]


@pytest.mark.parametrize("task_type,expected", _EXPECTED)
def test_resolve_handler_maps_every_known_type(task_type, expected):
    assert resolve_handler(task_type) is expected


def test_work_prefix_resolves_to_work_loop_but_not_over_exact_types():
    # A generic work.* type → the unified work loop.
    for t in ("work.task", "work.demo", "work.something.else"):
        assert resolve_handler(t) is _dispatch_work
    # But work.code (an EXACT coding type) MUST win over the work. prefix — the
    # specific-before-prefix invariant (§14). It resolves to the coding path.
    assert resolve_handler("work.code") is _dispatch_code
    assert _dispatch_code is not _dispatch_work


@pytest.mark.parametrize("task_type", ["mystery.thing", "", "capacity", "researchX", "prototyp"])
def test_unknown_types_resolve_to_none(task_type):
    assert resolve_handler(task_type) is None


# ===========================================================================
# (b) The newly-registered Capacity Steward now dispatches (was abandoned)
# ===========================================================================


def test_capacity_review_dispatches_and_enqueues_nothing(tmp_path):
    """A ``capacity.review`` task routes to run_capacity_steward via the injectable
    seam, returns kind="capacity"/outcome="done", heartbeats, reaches MERGED, and
    NEVER enqueues a follow-on task (no loop). Before ADR-0031 this type had no
    dispatcher and hit the unknown→abandoned fallback."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    called = {}

    task = q.enqueue(None, workstream="test", type=CAPACITY_REVIEW_TYPE, payload={})
    before = len(q.tasks)

    def fake_run_capacity(conn, s, *, workstream, task_id):
        # A capacity task must never receive an `enqueue` seam (no loop); it is scoped
        # to the claimed task's workstream and carries the task id for event linkage.
        called["workstream"] = workstream
        called["task_id"] = task_id
        return CapacityReport(
            workstreams_checked=1,
            flags=[CapacityFlag(
                workstream=workstream, period="daily", zone="warn",
                action="compact", projected_breach=False,
            )],
        )

    r = run_once(None, "w1", sink, registry=reg, config=load_policy(),
                 run_capacity=fake_run_capacity, **_seams(q))

    assert called["workstream"] == "test"       # scoped to the claimed workstream
    assert called["task_id"] == task.id
    assert r is not None
    assert r.kind == "capacity" and r.outcome == "done"
    assert "flagged 1 cap" in r.detail          # surfaces the flag + recommendation
    assert q.tasks[task.id].status is TaskStatus.MERGED
    assert task.id in q.heartbeats              # heartbeated like the other roles
    assert len(q.tasks) == before               # no follow-on task enqueued (no loop)


def test_capacity_review_quiet_workstream_still_merges(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    task = q.enqueue(None, workstream="test", type=CAPACITY_REVIEW_TYPE, payload={})

    def fake_quiet(conn, s, *, workstream, task_id):
        return CapacityReport(workstreams_checked=2, flags=[])

    r = run_once(None, "w1", sink, registry=reg, config=load_policy(),
                 run_capacity=fake_quiet, **_seams(q))
    assert r.kind == "capacity" and r.outcome == "done"
    assert "no capacity concern" in r.detail
    assert q.tasks[task.id].status is TaskStatus.MERGED


# ===========================================================================
# (c) The PM can enqueue a role task, and it later dispatches
# ===========================================================================


def test_pm_enqueue_role_task_then_dispatches(tmp_path):
    """The PM commissions a specialist role via enqueue_role_task; a later run_once
    claims that task and dispatches it through the role-agnostic registry (not the
    abandoned fallback). Closes the producer gap the stakeholder flagged."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)

    # PM enqueues a sourcing task by judgment (queue-only; no agent-to-agent call).
    created = enqueue_role_task(
        None, workstream="test", task_type=SOURCING_TASK_TYPES[0], enqueue=q.enqueue,
    )
    assert created.type == SOURCING_TASK_TYPES[0]
    assert q.tasks[created.id].status is TaskStatus.UP_FOR_GRABS

    # The worker claims + dispatches it (fake sourcing seam so it's keyless).
    from runtime.roles.sourcing import SourcingResult

    def fake_sourcing(conn, t, s, **kw):
        assert "enqueue" not in kw  # a role task never gets an enqueue seam (no loop)
        return SourcingResult(candidate_count=1, model_ids=["m1"],
                              provenance_hash="deadbeef", decision="autoadopt",
                              autoadopted=True)

    r = run_once(None, "w1", sink, registry=reg, config=load_policy(),
                 run_sourcing=fake_sourcing, **_seams(q))
    assert r is not None and r.kind == "sourcing" and r.outcome == "done"
    assert q.tasks[created.id].status is TaskStatus.MERGED


def test_enqueue_role_task_rejects_unknown_type():
    with pytest.raises(ValueError):
        enqueue_role_task(None, workstream="test", task_type="work.task",
                          enqueue=lambda *a, **k: None)


def test_every_pm_commissionable_type_has_a_live_handler():
    """Consistency invariant: every role type the PM is told it can enqueue actually
    resolves to a real handler — so the PM can never commission a role that the worker
    would only abandon."""
    for task_type in PM_ROLE_TASK_TYPES:
        assert resolve_handler(task_type) is not None, task_type


def test_role_catalog_note_lists_every_commissionable_type():
    note = role_catalog_note()
    for task_type in PM_ROLE_TASK_TYPES:
        assert f"`{task_type}`" in note
