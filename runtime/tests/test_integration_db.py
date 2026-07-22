"""Integration tests against a real Postgres.

These SKIP cleanly (never error, never hang) when no database is reachable — the
off-host sandbox has none. A short-timeout probe decides at collection time.
Run against a live DB with, e.g.:

    docker compose up -d postgres
    python -m runtime.migrate
    pytest runtime/tests/test_integration_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import append_event, read_events
from runtime.migrate import migrate
from runtime.models import Assignee, TaskStatus, make_event
from runtime.tasks import (
    claim_task,
    complete_task,
    enqueue_task,
    find_stale_tasks,
    heartbeat,
)

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
    # Unique workstream per test → isolation without deleting shared rows.
    return f"test-{uuid4().hex[:12]}"


def test_append_and_read_events(conn, ws):
    e1 = append_event(conn, make_event(workstream=ws, type="a", payload={"n": 1}))
    e2 = append_event(conn, make_event(workstream=ws, type="b", trace_id="tr"))
    got = read_events(conn, workstream=ws)
    assert [e.id for e in got] == [e1.id, e2.id]
    assert got[0].payload == {"n": 1}
    assert got[1].trace_id == "tr"


def test_read_events_since_filter(conn, ws):
    e1 = append_event(conn, make_event(workstream=ws, type="a"))
    e2 = append_event(conn, make_event(workstream=ws, type="b"))
    after = read_events(conn, workstream=ws, since=e1.ts)
    assert e2.id in {e.id for e in after}
    assert e1.id not in {e.id for e in after}


def test_enqueue_emits_created_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="build", payload={"x": 1}, priority=5)
    assert t.status is TaskStatus.QUEUED
    events = read_events(conn, task_id=t.id)
    assert [e.type for e in events] == ["task.created"]


def test_claim_picks_highest_priority(conn, ws):
    enqueue_task(conn, workstream=ws, type="low", priority=1)
    hi = enqueue_task(conn, workstream=ws, type="high", priority=9)
    claimed = claim_task(conn, worker_id="w1", workstream=ws)
    assert claimed is not None
    assert claimed.id == hi.id
    assert claimed.status is TaskStatus.IN_PROGRESS
    assert claimed.claimed_by == "w1"
    assert claimed.heartbeat_at is not None
    types = [e.type for e in read_events(conn, task_id=hi.id)]
    assert types == ["task.created", "task.claimed"]


def test_claim_respects_assignee_targeting(conn, ws):
    enqueue_task(conn, workstream=ws, type="host-only", assignee=Assignee.HOST)
    # An offhost worker must not claim a host-targeted task.
    assert claim_task(conn, worker_id="w-off", assignee=Assignee.OFFHOST, workstream=ws) is None
    # A host worker can.
    claimed = claim_task(conn, worker_id="w-host", assignee=Assignee.HOST, workstream=ws)
    assert claimed is not None and claimed.assignee is Assignee.HOST


def test_claim_returns_none_when_empty(conn, ws):
    assert claim_task(conn, worker_id="w1", workstream=ws) is None


def test_heartbeat_only_by_holder(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    assert heartbeat(conn, t.id, "someone-else") is None
    beat = heartbeat(conn, t.id, "w1")
    assert beat is not None and beat.heartbeat_at is not None


def test_heartbeat_emits_no_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    heartbeat(conn, t.id, "w1")
    heartbeat(conn, t.id, "w1")
    # High-frequency liveness must not bloat the append-only log (ADR-0013).
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types == ["task.created", "task.claimed"]
    assert "task.heartbeat" not in types


def test_complete_task_sets_result_and_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    done = complete_task(conn, t.id, result={"ok": True}, spent_tokens=42)
    assert done.status is TaskStatus.DONE
    assert done.result == {"ok": True}
    assert done.spent_tokens == 42
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types[-1] == "task.finished"


def test_complete_rejects_non_terminal_status(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    with pytest.raises(ValueError):
        complete_task(conn, t.id, status=TaskStatus.QUEUED)


def test_complete_guard_rejects_non_in_progress_task(conn, ws):
    # A queued (unclaimed) task cannot be finalized by default; no event emitted.
    t = enqueue_task(conn, workstream=ws, type="t")
    assert complete_task(conn, t.id, result={"x": 1}) is None
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types == ["task.created"]


def test_complete_guard_blocks_double_finalize(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    assert complete_task(conn, t.id, status=TaskStatus.DONE) is not None
    # Second finalize conflicts (already done) → None, no extra finished event.
    assert complete_task(conn, t.id, status=TaskStatus.FAILED) is None
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types.count("task.finished") == 1


def test_complete_force_bypasses_guard(conn, ws):
    # Supervisor force-fails a stale/re-kicked (still queued) task.
    t = enqueue_task(conn, workstream=ws, type="t")
    done = complete_task(
        conn, t.id, status=TaskStatus.FAILED, result={"reason": "stale"}, force=True
    )
    assert done is not None and done.status is TaskStatus.FAILED
    assert done.result == {"reason": "stale"}
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types[-1] == "task.finished"


def test_complete_missing_task_returns_none(conn, ws):
    assert complete_task(conn, uuid4(), force=True) is None


def test_find_stale_tasks(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    # Backdate the heartbeat to simulate a dropped worker.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET heartbeat_at = now() - interval '300 seconds' WHERE id = %s",
                (t.id,),
            )
    stale_ids = {s.id for s in find_stale_tasks(conn, threshold_seconds=60)}
    assert t.id in stale_ids
    # Not stale under a large threshold.
    assert t.id not in {s.id for s in find_stale_tasks(conn, threshold_seconds=10_000)}
