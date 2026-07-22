"""Unit tests for the scheduler's PM pulse — no database.

`tick_once` is exercised with injected `pending`/`enqueue` fakes so the
enqueue-vs-skip decision is tested without a DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from runtime.models import Task, TaskStatus
from runtime.scheduler import PM_TICK_TYPE, tick_once

CONN = object()


def _fake_enqueue(conn, *, workstream, type, payload=None, **kw) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(),
        workstream=workstream,
        type=type,
        status=TaskStatus.QUEUED,
        priority=0,
        payload=payload or {},
        created_at=now,
        updated_at=now,
    )


def test_tick_enqueues_when_none_pending():
    calls = []

    def enqueue(conn, **kw):
        calls.append(kw)
        return _fake_enqueue(conn, **kw)

    task = tick_once(CONN, pending=lambda c, ws: False, enqueue=enqueue)
    assert task is not None
    assert task.type == PM_TICK_TYPE
    assert task.workstream == "productivity"
    assert len(calls) == 1


def test_tick_skips_when_already_pending():
    def enqueue(conn, **kw):  # pragma: no cover - must not be called
        raise AssertionError("must not enqueue when a pm.tick is already pending")

    assert tick_once(CONN, pending=lambda c, ws: True, enqueue=enqueue) is None


def test_tick_respects_workstream_arg():
    seen = {}

    def pending(conn, ws):
        seen["ws"] = ws
        return False

    task = tick_once(CONN, workstream="vertical-x", pending=pending, enqueue=_fake_enqueue)
    assert seen["ws"] == "vertical-x"
    assert task is not None and task.workstream == "vertical-x"
