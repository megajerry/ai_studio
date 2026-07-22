"""Concurrency / robustness e2e tests against a real Postgres (ADR-0004).

These prove the queue's two liveness guarantees under real concurrency:

1. ``claim_task`` (``FOR UPDATE SKIP LOCKED``) never lets two workers claim the
   same task — many worker threads drain a queue with no double-claim and no
   lost task.
2. The non-agent supervisor re-kicks a genuinely stalled task (silent worker),
   escalating to a force-fail once retries are exhausted, and — per the fix in
   this pass — never clobbers a task that self-completed first.
3. A small end-to-end: two workers service ``work.*`` tasks and every task
   reaches a terminal state exactly once (no lost / duplicated work).

Each thread owns its OWN connection (psycopg connections are not thread-safe),
loops are bounded (never hang), and the whole module SKIPS cleanly when no
DATABASE_URL is reachable (off-host sandbox), matching the other DB tests.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from runtime import db
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.supervisor import sweep
from runtime.tasks import claim_task, complete_task, enqueue_task

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure schema exists
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    # Unique workstream per test → isolation without touching shared rows.
    return f"conc-{uuid4().hex[:12]}"


def _fresh(conn):
    """End any open implicit transaction so the next read sees other threads' commits."""
    conn.commit()


# ---------------------------------------------------------------------------
# 1. No double-claim under concurrency (SKIP LOCKED correctness)
# ---------------------------------------------------------------------------


def test_no_double_claim_under_concurrent_workers(conn, ws):
    M = 30  # tasks
    K = 6   # concurrent workers
    enqueued = {
        enqueue_task(conn, workstream=ws, type="work.item", payload={"i": i}).id
        for i in range(M)
    }
    _fresh(conn)  # commit so the worker connections can see the enqueued rows
    assert len(enqueued) == M

    def drain(worker_id: str) -> list:
        """Claim until the queue looks empty; own connection; bounded iterations."""
        wconn = db.connect()
        claimed: list = []
        try:
            misses = 0
            for _ in range(M * 4):  # hard bound: never hang
                task = claim_task(wconn, worker_id=worker_id, workstream=ws)
                if task is None:
                    misses += 1
                    if misses >= 3:  # queue drained (only locked rows remain)
                        break
                    time.sleep(0.01)
                    continue
                misses = 0
                claimed.append(task.id)
        finally:
            wconn.close()
        return claimed

    with ThreadPoolExecutor(max_workers=K) as pool:
        results = list(pool.map(drain, [f"w{i}" for i in range(K)]))

    all_claimed = [tid for r in results for tid in r]
    # No task claimed twice (SKIP LOCKED + FOR UPDATE), and every task claimed once.
    assert len(all_claimed) == len(set(all_claimed)), "a task was claimed more than once"
    assert set(all_claimed) == enqueued, "some task was never claimed (lost)"
    assert len(all_claimed) == M

    # Every row is now in_progress under exactly one distinct worker.
    _fresh(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, claimed_by FROM tasks WHERE workstream = %s", (ws,)
        )
        rows = cur.fetchall()
    _fresh(conn)
    assert all(r["status"] == "in_progress" for r in rows)
    assert all(r["claimed_by"] is not None for r in rows)


# ---------------------------------------------------------------------------
# 2. Supervisor re-kick of a real stall → escalates to force-fail
# ---------------------------------------------------------------------------


def _backdate_heartbeat(conn, task_id, seconds=300):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
                (seconds, task_id),
            )


def test_supervisor_rekicks_real_stall_then_force_fails(conn, ws):
    # Worker A claims but goes silent (never heartbeats).
    conn_a = db.connect()
    conn_b = db.connect()
    try:
        t = enqueue_task(conn, workstream=ws, type="work.stall")
        _fresh(conn)  # commit so worker connections A/B can see it
        claimed_a = claim_task(conn_a, worker_id="A", workstream=ws)
        assert claimed_a is not None and claimed_a.id == t.id

        # Its heartbeat is stale → the supervisor re-kicks it (retries 0 -> 1).
        _backdate_heartbeat(conn, t.id)
        res1 = sweep(conn, threshold_s=60, max_retries=2)
        assert t.id in res1.rekicked

        # Back to queued and re-claimable by a DIFFERENT worker (B).
        _fresh(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT status, retries FROM tasks WHERE id = %s", (t.id,))
            row = cur.fetchone()
        _fresh(conn)
        assert row["status"] == "up_for_grabs" and row["retries"] == 1

        claimed_b = claim_task(conn_b, worker_id="B", workstream=ws)
        assert claimed_b is not None and claimed_b.id == t.id

        # B also stalls; retries (1) still < max (2) → second re-kick (-> 2).
        _backdate_heartbeat(conn, t.id)
        res2 = sweep(conn, threshold_s=60, max_retries=2)
        assert t.id in res2.rekicked

        # Re-claim; now stale with retries (2) >= max (2) → force-failed.
        _fresh(conn)  # commit the second re-kick so B2 can re-claim
        claimed_c = claim_task(conn_b, worker_id="B2", workstream=ws)
        assert claimed_c is not None and claimed_c.id == t.id
        _backdate_heartbeat(conn, t.id)
        res3 = sweep(conn, threshold_s=60, max_retries=2)
        assert t.id in res3.failed

        _fresh(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tasks WHERE id = %s", (t.id,))
            assert cur.fetchone()["status"] == "abandoned"
        _fresh(conn)
        types = [e.type for e in read_events(conn, task_id=t.id)]
        assert types.count("task.rekicked") == 2
        assert "task.failed_exhausted" in types
    finally:
        conn_a.close()
        conn_b.close()


def test_supervisor_does_not_clobber_task_completed_before_sweep(conn, ws):
    # Per fix (b): a task at max retries that completes in the scan->write window
    # is NOT force-failed over its terminal `done` state.
    t = enqueue_task(conn, workstream=ws, type="work.race")
    claimed = claim_task(conn, worker_id="A", workstream=ws)
    assert claimed is not None

    # Supervisor's scan snapshot: in_progress at max retries (would force-fail).
    snapshot = claimed.model_copy(update={"retries": 2})
    # ...but the worker finishes first.
    assert complete_task(conn, t.id, status=TaskStatus.MERGED, result={"ok": 1}) is not None

    res = sweep(conn, threshold_s=60, max_retries=2, find_stale=lambda c, s: [snapshot])
    assert t.id not in res.failed

    _fresh(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM tasks WHERE id = %s", (t.id,))
        row = cur.fetchone()
    _fresh(conn)
    assert row["status"] == "merged" and row["result"] == {"ok": 1}
    assert "task.failed_exhausted" not in [e.type for e in read_events(conn, task_id=t.id)]


# ---------------------------------------------------------------------------
# 3. Small end-to-end: two workers, every task terminal exactly once
# ---------------------------------------------------------------------------


def test_two_workers_each_task_terminal_exactly_once(conn, ws):
    N = 12
    enqueued = {
        enqueue_task(conn, workstream=ws, type=f"work.{i % 3}", payload={"i": i}).id
        for i in range(N)
    }
    _fresh(conn)  # commit so the worker connections can see the enqueued rows
    assert len(enqueued) == N

    completed_lock = threading.Lock()
    completed: list = []

    def work(worker_id: str) -> None:
        wconn = db.connect()
        try:
            misses = 0
            for _ in range(N * 4):  # bounded
                task = claim_task(wconn, worker_id=worker_id, workstream=ws)
                if task is None:
                    misses += 1
                    if misses >= 3:
                        break
                    time.sleep(0.01)
                    continue
                misses = 0
                done = complete_task(
                    wconn, task.id, status=TaskStatus.MERGED, result={"by": worker_id}
                )
                # The claimer always owns the finalize (guarded to in_progress).
                assert done is not None and done.status is TaskStatus.MERGED
                with completed_lock:
                    completed.append(task.id)
        finally:
            wconn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, ["wA", "wB"]))

    # Every task completed exactly once, none lost or duplicated.
    assert len(completed) == N
    assert set(completed) == enqueued
    assert len(set(completed)) == N

    _fresh(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM tasks WHERE workstream = %s", (ws,)
        )
        statuses = [r["status"] for r in cur.fetchall()]
    _fresh(conn)
    assert statuses == ["merged"] * N

    # Exactly one task.finished event per task (no double-finalize).
    for tid in enqueued:
        types = [e.type for e in read_events(conn, task_id=tid)]
        assert types.count("task.finished") == 1
