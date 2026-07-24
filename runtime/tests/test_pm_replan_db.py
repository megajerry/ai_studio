"""Live-DB tests for PM re-decomposition of a stuck task (ADR-0023, R2).

Exercises the R2 loop end-to-end against a real Postgres: the supervisor's
``task.stuck`` SIGNAL is turned into a PM ``replan`` task by the QUEUE consumer
(``scheduler.dispatch_replans``) — never an agent-to-agent call — the PM reads the
preserved abandoned spec and enqueues N SMALLER subtasks (a DAG), emits a body-free
``task.replanned``, and the original stays abandoned. The replan is BOUNDED: at the
depth cap it escalates to a human 🛑 instead of re-decomposing forever. SKIP cleanly
when no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_pm_replan_db.py
"""

from __future__ import annotations

import json
import os
import tempfile
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("MODELS_DRY_RUN", "1")  # keyless: dry-run every model call

from runtime import db
from runtime.enforce import DbEventSink
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.policy import load_policy
from runtime.roles.pm import run_pm_replan
from runtime.scheduler import dispatch_replans
from runtime.tasks import (
    claim_task,
    enqueue_task,
    escalate_stuck_task,
    get_task,
)
from runtime.worker import build_registry, run_once

pytestmark = pytest.mark.skipif(
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
    return f"replan-{uuid4().hex[:12]}"


# --- helpers ----------------------------------------------------------------


def _max_seq(conn) -> int:
    """Current high-water event ``seq`` — a baseline cursor scoped to THIS test."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(seq), 0) AS s FROM events")
        s = int(cur.fetchone()["s"])
    conn.commit()
    return s


def _work_count(conn, ws: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
            (ws,),
        )
        n = int(cur.fetchone()["n"])
    conn.commit()
    return n


def _types(conn, task_id) -> list[str]:
    return [e.type for e in read_events(conn, task_id=task_id)]


def _make_stuck(conn, ws: str, *, payload: dict) -> UUID:
    """Enqueue a work task, claim it, and supersede it as stuck (R1) → abandoned."""
    t = enqueue_task(conn, workstream=ws, type="work.task", payload=payload)
    claim_task(conn, worker_id="w1", workstream=ws)  # → in_progress
    escalate_stuck_task(
        conn, t.id, stall_reason="no_progress", no_progress_rekicks=2, retries=2
    )
    assert get_task(conn, t.id).status == TaskStatus.ABANDONED
    return t.id


class _Completion:
    def __init__(self, text: str):
        self.text = text


def _plan_json(items: list[dict]) -> str:
    return json.dumps(
        {
            "restated_goal": "g",
            "success_criteria": ["all subtasks complete"],
            "confidence": 0.9,
            "feasible": True,
            "reason": "",
            "work_items": items,
        }
    )


# --- 1. task.stuck signal → replan task enqueued via the QUEUE (no direct call)


def test_stuck_signal_enqueues_replan_task_idempotently(conn, ws):
    base = _max_seq(conn)
    stuck_id = _make_stuck(conn, ws, payload={"goal": "big thing", "criterion": "c", "marker": "m"})

    cursor, ids = dispatch_replans(conn, since_seq=base)
    assert len(ids) == 1  # exactly one replan task for the one stuck task
    assert cursor > base  # cursor advanced past the scanned task.stuck event

    replan = get_task(conn, UUID(ids[0]))
    assert replan.type == "replan"
    assert replan.status == TaskStatus.UP_FOR_GRABS  # grabbable by the PM worker
    assert replan.workstream == ws
    assert replan.payload["stuck_task_id"] == str(stuck_id)

    # Idempotent: re-scanning from the SAME baseline enqueues NO duplicate replan.
    _, ids2 = dispatch_replans(conn, since_seq=base)
    assert ids2 == []


# --- 2. run_pm_replan reads the spec → ≥2 SMALLER subtasks + body-free event -


def test_run_pm_replan_decomposes_into_smaller_subtasks(conn, ws):
    stuck_id = _make_stuck(
        conn, ws,
        payload={"goal": "prove the studio re-decomposes stuck work",
                 "criterion": "c", "marker": "m"},
    )
    sink = DbEventSink(conn)
    res = run_pm_replan(conn, stuck_id, sink)

    assert res.decision == "replanned"
    assert res.subtask_count >= 2  # broken into SMALLER pieces, not one monolith
    assert len(res.subtask_ids) == res.subtask_count

    # The original stays abandoned (superseded), never resurrected.
    assert get_task(conn, stuck_id).status == TaskStatus.ABANDONED

    # Every subtask links back to the original + carries a bumped replan depth.
    for sid in res.subtask_ids:
        s = get_task(conn, UUID(sid))
        assert s.workstream == ws
        assert s.status == TaskStatus.UP_FOR_GRABS
        assert s.payload["replan_of"] == str(stuck_id)
        assert s.payload["parent_task_id"] == str(stuck_id)
        assert s.payload["replan_depth"] == 1

    # task.replanned is body-free (ids + counts only).
    ev = [e for e in read_events(conn, task_id=stuck_id) if e.type == "task.replanned"][0]
    assert set(ev.payload) <= {"subtask_ids", "subtask_count", "replan_depth"}
    assert ev.payload["subtask_count"] == res.subtask_count
    assert ev.payload["replan_depth"] == 1

    # The new subtasks are grabbable (independent → claimable in parallel).
    grabbed = claim_task(conn, worker_id="w2", workstream=ws, filter={"type": "work.task"})
    assert grabbed is not None and str(grabbed.id) in res.subtask_ids


def test_run_pm_replan_dag_edges_map_to_subtask_ids(conn, ws):
    """A plan with an ordering edge (item 2 depends on item 1) is enqueued as a real
    DAG: subtask 2's depends_on holds subtask 1's id (dependents wait)."""
    stuck_id = _make_stuck(conn, ws, payload={"goal": "ordered work", "criterion": "c", "marker": "m"})

    def _fake_call(**kw):
        return _Completion(_plan_json([
            {"title": "A", "type": "work.task", "instructions": "do A",
             "success_criterion": "A has marker", "marker": "ma", "depends_on": []},
            {"title": "B", "type": "work.task", "instructions": "do B",
             "success_criterion": "B has marker", "marker": "mb", "depends_on": [1]},
        ]))

    res = run_pm_replan(conn, stuck_id, DbEventSink(conn), call_model=_fake_call)
    assert res.subtask_count == 2
    first_id, second_id = res.subtask_ids  # reported in the plan's item order
    first = get_task(conn, UUID(first_id))
    second = get_task(conn, UUID(second_id))
    assert first.depends_on == []
    assert second.depends_on == [UUID(first_id)]  # edge mapped to the real task id


# --- 3. BOUNDED: at the depth cap → escalate 🛑, do NOT re-decompose (no loop) -


def test_replan_depth_cap_escalates_and_does_not_recurse(conn, ws):
    # A stuck task already AT the replan-depth cap (2).
    stuck_id = _make_stuck(
        conn, ws,
        payload={"goal": "keeps getting stuck", "criterion": "c", "marker": "m",
                 "replan_depth": 2},
    )
    before = _work_count(conn, ws)  # just the original
    sink = DbEventSink(conn)

    res = run_pm_replan(conn, stuck_id, sink, max_depth=2)
    assert res.decision == "escalated"
    assert res.subtask_count == 0 and res.subtask_ids == []
    assert res.approval_id  # a 🛑 human approval was raised
    assert _work_count(conn, ws) == before  # NO new subtasks enqueued

    # A SECOND replan attempt at the cap still escalates (never re-decomposes) —
    # this is the infinite-replan guard.
    res2 = run_pm_replan(conn, stuck_id, sink, max_depth=2)
    assert res2.decision == "escalated" and res2.subtask_count == 0
    assert _work_count(conn, ws) == before  # STILL no re-decomposition

    # task.replan_escalated is body-free.
    ev = [e for e in read_events(conn, task_id=stuck_id) if e.type == "task.replan_escalated"][0]
    assert set(ev.payload) <= {"replan_depth", "max_depth", "approval_id"}
    assert ev.payload["replan_depth"] == 2 and ev.payload["max_depth"] == 2


def test_subtasks_inherit_original_trajectory(conn, ws):
    """When the stuck task was born from a reasoning trajectory (ADR-0020), its
    replan subtasks inherit that ``trajectory_id`` so the outcome stays attributable."""
    from runtime.trajectory import start_trajectory

    tid = start_trajectory(conn, "pm", ws, "original decomposition goal")
    t = enqueue_task(
        conn, workstream=ws, type="work.task",
        payload={"goal": "linked work", "criterion": "c", "marker": "m"},
        trajectory_id=tid,
    )
    claim_task(conn, worker_id="w1", workstream=ws)
    escalate_stuck_task(conn, t.id, stall_reason="no_progress", no_progress_rekicks=2, retries=2)

    res = run_pm_replan(conn, t.id, DbEventSink(conn))
    assert res.subtask_count >= 2
    for sid in res.subtask_ids:
        with conn.cursor() as cur:
            cur.execute("SELECT trajectory_id FROM tasks WHERE id = %s", (UUID(sid),))
            got = cur.fetchone()["trajectory_id"]
        conn.commit()
        assert got == tid


def test_run_pm_replan_missing_stuck_task_is_noop(conn, ws):
    res = run_pm_replan(conn, uuid4(), DbEventSink(conn))
    assert res.missing is True and res.subtask_ids == []


# --- 4. End-to-end through the worker dispatch ------------------------------


def test_worker_dispatches_replan_task_end_to_end(conn, ws):
    base = _max_seq(conn)
    stuck_id = _make_stuck(conn, ws, payload={"goal": "worker replan", "criterion": "c", "marker": "m"})
    _, ids = dispatch_replans(conn, since_seq=base)
    assert len(ids) == 1

    scratch = tempfile.mkdtemp(prefix="ai_studio_replan_")
    r = run_once(
        conn, "replan-worker", DbEventSink(conn),
        registry=build_registry(scratch), config=load_policy(), workstream=ws,
    )
    assert r is not None and r.kind == "replan" and r.outcome == "done"

    # The replan task merged; the PM enqueued ≥2 smaller subtasks for the original.
    replan = get_task(conn, UUID(ids[0]))
    assert replan.status == TaskStatus.MERGED
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s "
            "AND payload->>'replan_of' = %s",
            (ws, str(stuck_id)),
        )
        n_subs = int(cur.fetchone()["n"])
    conn.commit()
    assert n_subs >= 2
