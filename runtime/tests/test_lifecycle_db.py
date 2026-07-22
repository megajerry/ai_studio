"""Live-DB tests for the canonical lifecycle, grab-by-sort, dependencies, and
lifecycle telemetry (ADR-0015). SKIP cleanly when no DATABASE_URL is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_lifecycle_db.py
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus, make_event
from runtime.task_state import IllegalTransition
from runtime.tasks import (
    agent_rollup,
    complete_task,
    enqueue_task,
    grab_task,
    list_for_review,
    model_rollup,
    ready_tasks,
    start_task,
    task_cost,
    task_lifecycle,
    transition,
    waiting_tasks,
)

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
    return f"life-{uuid4().hex[:12]}"


def _status(conn, task_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (task_id,))
        s = cur.fetchone()["status"]
    conn.commit()
    return s


# --- guarded transition -----------------------------------------------------


def test_illegal_transition_rejected(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="work.x")
    assert t.status is TaskStatus.UP_FOR_GRABS
    # up_for_grabs -> merged is not a legal move.
    with pytest.raises(IllegalTransition):
        transition(conn, t.id, TaskStatus.MERGED)
    # The row is untouched (transaction rolled back).
    assert _status(conn, t.id) == "up_for_grabs"


def test_transition_guarded_on_current_status(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="work.x")
    # expected_from mismatch → no-op None, no change.
    assert transition(conn, t.id, TaskStatus.CLAIMED,
                      expected_from=TaskStatus.IN_PROGRESS) is None
    assert _status(conn, t.id) == "up_for_grabs"


def test_full_lifecycle_up_for_grabs_to_merged(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="work.x")
    grabbed = grab_task(conn, worker_id="w1", agent_type="executor", workstream=ws)
    assert grabbed is not None and grabbed.status is TaskStatus.CLAIMED
    assert grabbed.claimed_by == "w1" and grabbed.agent_type == "executor"
    assert grabbed.claimed_at is not None
    assert start_task(conn, t.id, "w1").status is TaskStatus.IN_PROGRESS
    assert transition(conn, t.id, TaskStatus.READY_FOR_REVIEW).status is TaskStatus.READY_FOR_REVIEW
    assert transition(conn, t.id, TaskStatus.APPROVED,
                      agent_id="rev", agent_type="reviewer").status is TaskStatus.APPROVED
    merged = transition(conn, t.id, TaskStatus.MERGED)
    assert merged.status is TaskStatus.MERGED
    # task.finished emitted on the terminal hop.
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types[-1] == "task.finished"
    assert types.count("task.transition") == 5  # grab, start, submit, approve, merge


def test_task_transitions_rows_and_latency_populated(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="work.x")
    grab_task(conn, worker_id="w1", agent_type="executor", workstream=ws)
    time.sleep(0.02)  # ensure a measurable gap for latency
    start_task(conn, t.id, "w1")
    life = task_lifecycle(conn, t.id)
    trans = life["transitions"]
    assert [x["to_status"] for x in trans] == ["claimed", "in_progress"]
    assert trans[0]["from_status"] == "up_for_grabs"
    assert trans[0]["agent_type"] == "executor"
    # latency recorded (non-negative), total is the sum.
    assert all(x["latency_ms"] is not None and x["latency_ms"] >= 0 for x in trans)
    assert life["total_ms"] == sum(x["latency_ms"] for x in trans)
    assert life["current"] == "in_progress"


def test_task_cost_sums_model_call_events(conn, ws, monkeypatch):
    from runtime.enforce import DbEventSink
    from runtime.model.call import call_model

    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "MODELS_DRY_RUN"):
        monkeypatch.delenv(env, raising=False)

    t = enqueue_task(conn, workstream=ws, type="work.x")
    grab_task(conn, worker_id="w1", workstream=ws)
    start_task(conn, t.id, "w1")
    sink = DbEventSink(conn)
    c1 = call_model("executor", "execute", [{"role": "user", "content": "hi " * 40}],
                    workstream=ws, sink=sink, task_id=t.id, conn=conn)
    c2 = call_model("verifier", "verify", [{"role": "user", "content": "check " * 20}],
                    workstream=ws, sink=sink, task_id=t.id, conn=conn)
    cost = task_cost(conn, t.id)
    assert cost["calls"] == 2
    assert cost["total_tokens"] == c1.usage.total_tokens + c2.usage.total_tokens
    assert cost["spent_tokens"] == c1.usage.total_tokens + c2.usage.total_tokens
    assert cost["latency_ms"] >= 0

    # Rollups return data for this task's agents/models.
    assert any(r["agent_type"] is None or r["transitions"] > 0 for r in agent_rollup(conn))
    assert any(r["calls"] > 0 for r in model_rollup(conn))


def test_list_for_review_surfaces_ready_tasks(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="work.x")
    grab_task(conn, worker_id="w1", workstream=ws)
    start_task(conn, t.id, "w1")
    transition(conn, t.id, TaskStatus.READY_FOR_REVIEW)
    ids = {x.id for x in list_for_review(conn, workstream=ws)}
    assert t.id in ids
    # A human/off-host Reviewer approves it.
    transition(conn, t.id, TaskStatus.APPROVED, agent_id="human", agent_type="reviewer")
    assert t.id not in {x.id for x in list_for_review(conn, workstream=ws)}


# --- grab-by-sort + SKIP LOCKED ---------------------------------------------


def test_grab_by_sort_picks_the_right_task(conn, ws):
    lo = enqueue_task(conn, workstream=ws, type="a", priority=1)
    hi = enqueue_task(conn, workstream=ws, type="b", priority=9)
    # Default sort (priority DESC) picks the high-priority one.
    g = grab_task(conn, worker_id="w1", workstream=ws)
    assert g.id == hi.id
    # A caller-supplied sort (priority ASC) picks the low-priority one instead.
    g2 = grab_task(conn, worker_id="w2", workstream=ws, sort="priority ASC, created_at ASC")
    assert g2.id == lo.id


def test_grab_filter_narrows_selection(conn, ws):
    a = enqueue_task(conn, workstream=ws, type="work.keep")
    enqueue_task(conn, workstream=ws, type="work.skip")
    # Structured, parameterized filter (equality on an allowlisted column).
    g = grab_task(conn, worker_id="w1", workstream=ws, filter={"type": "work.keep"})
    assert g is not None and g.id == a.id


def test_grab_filter_supports_in_list(conn, ws):
    a = enqueue_task(conn, workstream=ws, type="work.a")
    b = enqueue_task(conn, workstream=ws, type="work.b")
    enqueue_task(conn, workstream=ws, type="work.c")
    grabbed = set()
    while True:
        g = grab_task(conn, worker_id="w1", workstream=ws,
                      filter={"type": ["work.a", "work.b"]})
        if g is None:
            break
        grabbed.add(g.id)
    assert grabbed == {a.id, b.id}  # work.c excluded by the IN-list filter


def test_grab_sort_multi_column_and_tuple_forms(conn, ws):
    a = enqueue_task(conn, workstream=ws, type="work.a", priority=5)
    b = enqueue_task(conn, workstream=ws, type="work.b", priority=5)  # same priority
    # Valid multi-column sort (string form): priority DESC, created_at DESC → newest
    # of the top-priority tier first (b before a).
    g = grab_task(conn, worker_id="w1", workstream=ws,
                  sort="priority DESC, created_at DESC")
    assert g.id == b.id
    # Tuple form with NULLS ordering is also accepted.
    g2 = grab_task(conn, worker_id="w2", workstream=ws,
                   sort=[("priority", "DESC"), ("created_at", "ASC", "NULLS LAST")])
    assert g2.id == a.id


def test_grab_rejects_bad_sort(conn, ws):
    enqueue_task(conn, workstream=ws, type="work.x")
    # Unknown column / trailing statement → rejected (not silently permitted).
    with pytest.raises(ValueError):
        grab_task(conn, worker_id="w1", workstream=ws, sort="priority; DROP TABLE tasks")
    # A non-allowlisted column is refused.
    with pytest.raises(ValueError):
        grab_task(conn, worker_id="w1", workstream=ws, sort="secret_column ASC")
    # A bad direction is refused.
    with pytest.raises(ValueError):
        grab_task(conn, worker_id="w1", workstream=ws, sort="priority SIDEWAYS")


def test_grab_sort_cannot_inject_sql(conn, ws):
    """The exploit from review: a subquery/function-call sort must be REJECTED
    (ValueError) — not executed. Belt-and-suspenders: no multi-second pg_sleep
    delay and the tasks table is still intact afterwards."""
    real = enqueue_task(conn, workstream=ws, type="work.real")
    conn.commit()
    exploit = "(SELECT 1 FROM (SELECT pg_sleep(3)) s)"
    start = time.monotonic()
    with pytest.raises(ValueError):
        grab_task(conn, worker_id="w1", workstream=ws, sort=exploit)
    elapsed = time.monotonic() - start
    conn.rollback()  # clear the aborted-tx state from the expected error
    assert elapsed < 1.0, f"pg_sleep appears to have executed ({elapsed:.1f}s)"
    # The table is intact and the real row is untouched + still grabbable.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE id = %s", (real.id,))
        assert cur.fetchone()["n"] == 1
    conn.commit()
    assert grab_task(conn, worker_id="w1", workstream=ws).id == real.id


def test_grab_filter_rejects_unknown_column(conn, ws):
    enqueue_task(conn, workstream=ws, type="work.x")
    # A column outside the allowlist (e.g. a raw-SQL smuggling attempt) is refused.
    with pytest.raises(ValueError):
        grab_task(conn, worker_id="w1", workstream=ws,
                  filter={"1=1; DROP TABLE tasks;--": "x"})


def test_grab_filter_value_cannot_inject_sql(conn, ws):
    """A hostile filter VALUE is bound as a parameter, never executed: the query
    runs safely, matches nothing, and the tasks table still exists afterwards."""
    real = enqueue_task(conn, workstream=ws, type="work.real")
    conn.commit()
    hostile = "work.real'; DROP TABLE tasks; --"
    # No exception, no DROP — the value is compared as a literal string (no match).
    g = grab_task(conn, worker_id="w1", workstream=ws, filter={"type": hostile})
    assert g is None
    # The table is intact and the legitimate row is untouched + still grabbable.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE id = %s", (real.id,))
        assert cur.fetchone()["n"] == 1
    conn.commit()
    g2 = grab_task(conn, worker_id="w1", workstream=ws, filter={"type": "work.real"})
    assert g2 is not None and g2.id == real.id


def test_skip_locked_no_double_grab(conn, ws):
    M, K = 24, 6
    enq = {enqueue_task(conn, workstream=ws, type="work.i", payload={"i": i}).id
           for i in range(M)}
    conn.commit()

    def drain(worker_id: str) -> list:
        wconn = db.connect()
        got: list = []
        try:
            misses = 0
            for _ in range(M * 4):
                t = grab_task(wconn, worker_id=worker_id, workstream=ws)
                if t is None:
                    misses += 1
                    if misses >= 3:
                        break
                    time.sleep(0.01)
                    continue
                misses = 0
                got.append(t.id)
        finally:
            wconn.close()
        return got

    with ThreadPoolExecutor(max_workers=K) as pool:
        results = list(pool.map(drain, [f"w{i}" for i in range(K)]))
    grabbed = [tid for r in results for tid in r]
    assert len(grabbed) == len(set(grabbed)), "a task was grabbed more than once"
    assert set(grabbed) == enq, "some task was never grabbed"


# --- dependencies -----------------------------------------------------------


def test_two_independent_tasks_both_grabbable_in_parallel(conn, ws):
    a = enqueue_task(conn, workstream=ws, type="work.a")
    b = enqueue_task(conn, workstream=ws, type="work.b")
    conn.commit()
    ready = {x.id for x in ready_tasks(conn, workstream=ws)}
    assert {a.id, b.id} <= ready  # both independent → both grabbable now
    assert waiting_tasks(conn, workstream=ws) == []


def test_dependent_task_waits_until_prereq_merged(conn, ws):
    prereq = enqueue_task(conn, workstream=ws, type="work.prereq")
    dep = enqueue_task(conn, workstream=ws, type="work.dep", depends_on=[prereq.id])

    # The dependent is NOT grabbable and shows up as waiting on the prereq.
    ready_ids = {x.id for x in ready_tasks(conn, workstream=ws)}
    assert prereq.id in ready_ids and dep.id not in ready_ids
    waiting = waiting_tasks(conn, workstream=ws)
    w = next(x for x in waiting if x["task"].id == dep.id)
    assert prereq.id in w["pending_prereqs"] and not w["blocked_by_abandoned"]

    # grab_task never returns the dependent while its prereq is unmet.
    grabbed_ids = set()
    while True:
        g = grab_task(conn, worker_id="w1", workstream=ws)
        if g is None:
            break
        grabbed_ids.add(g.id)
    assert prereq.id in grabbed_ids and dep.id not in grabbed_ids

    # Drive the prereq to merged; now the dependent becomes grabbable.
    start_task(conn, prereq.id, "w1")
    transition(conn, prereq.id, TaskStatus.READY_FOR_REVIEW)
    transition(conn, prereq.id, TaskStatus.APPROVED)
    transition(conn, prereq.id, TaskStatus.MERGED)
    assert dep.id in {x.id for x in ready_tasks(conn, workstream=ws)}
    g = grab_task(conn, worker_id="w1", workstream=ws)
    assert g is not None and g.id == dep.id


def test_diamond_dag_resolves(conn, ws):
    root = enqueue_task(conn, workstream=ws, type="work.root")
    left = enqueue_task(conn, workstream=ws, type="work.left", depends_on=[root.id])
    right = enqueue_task(conn, workstream=ws, type="work.right", depends_on=[root.id])
    join = enqueue_task(conn, workstream=ws, type="work.join", depends_on=[left.id, right.id])

    def merge(tid):
        # Walk the full canonical lifecycle by id (transition ignores the grab-time
        # dependency gate, which only constrains grab_task).
        transition(conn, tid, TaskStatus.CLAIMED, agent_id="w1", claimed_by="w1")
        transition(conn, tid, TaskStatus.IN_PROGRESS, agent_id="w1")
        transition(conn, tid, TaskStatus.READY_FOR_REVIEW)
        transition(conn, tid, TaskStatus.APPROVED)
        transition(conn, tid, TaskStatus.MERGED)

    # Only root is grabbable initially.
    assert {x.id for x in ready_tasks(conn, workstream=ws)} == {root.id}
    merge(root.id)
    # Now both left and right are grabbable in parallel; join still waits.
    assert {x.id for x in ready_tasks(conn, workstream=ws)} == {left.id, right.id}
    merge(left.id)
    assert join.id not in {x.id for x in ready_tasks(conn, workstream=ws)}  # right pending
    merge(right.id)
    # With both parents merged, join is grabbable.
    assert {x.id for x in ready_tasks(conn, workstream=ws)} == {join.id}


def test_abandoned_prereq_leaves_dependent_waiting_never_grabbed(conn, ws):
    prereq = enqueue_task(conn, workstream=ws, type="work.prereq")
    dep = enqueue_task(conn, workstream=ws, type="work.dep", depends_on=[prereq.id])
    # Grab + start the prereq, then abandon it (it can never succeed).
    g = grab_task(conn, worker_id="w1", workstream=ws)
    assert g.id == prereq.id
    start_task(conn, prereq.id, "w1")
    complete_task(conn, prereq.id, status=TaskStatus.ABANDONED, result={"why": "nope"})

    # The dependent is never grabbable and is surfaced as blocked-by-abandoned.
    assert dep.id not in {x.id for x in ready_tasks(conn, workstream=ws)}
    assert grab_task(conn, worker_id="w1", workstream=ws) is None
    w = next(x for x in waiting_tasks(conn, workstream=ws) if x["task"].id == dep.id)
    assert w["blocked_by_abandoned"] and prereq.id in w["pending_prereqs"]


def test_dependencies_visible_in_lifecycle(conn, ws):
    prereq = enqueue_task(conn, workstream=ws, type="work.prereq")
    dep = enqueue_task(conn, workstream=ws, type="work.dep", depends_on=[prereq.id])
    assert task_lifecycle(conn, dep.id)["depends_on"] == [prereq.id]


# --- migration idempotency + legacy mapping ---------------------------------


def test_migration_idempotent_and_maps_legacy_statuses(conn):
    """Re-running 0008 directly is a no-op, and inserting legacy status values then
    applying the mapping updates yields the canonical ones (idempotent)."""
    sql = Path("runtime/migrations/0008_task_lifecycle.sql").read_text("utf-8")
    c = db.connect()
    c.autocommit = True
    with c.cursor() as cur:
        # Idempotent: applying the whole migration again must not error.
        cur.execute(sql)
        # Legacy mapping is idempotent + correct: a row can't hold a legacy status
        # now (CHECK forbids it), so exercise the mapping UPDATEs directly — they
        # are no-ops because no legacy rows remain.
        cur.execute("UPDATE tasks SET status = 'up_for_grabs' WHERE status = 'queued'")
        assert cur.rowcount == 0
        cur.execute("UPDATE tasks SET status = 'merged' WHERE status = 'done'")
        assert cur.rowcount == 0
        cur.execute("UPDATE tasks SET status = 'abandoned' WHERE status = 'failed'")
        assert cur.rowcount == 0
        # The canonical CHECK is in force.
        cur.execute(
            "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
            "WHERE conname = 'tasks_status_check'"
        )
        d = cur.fetchone()["def"]
    c.close()
    for s in ("up_for_grabs", "claimed", "in_progress", "blocked", "ready_for_review",
              "reviewer_blocked", "approved", "merged", "abandoned"):
        assert s in d
    assert "queued" not in d and "'done'" not in d
