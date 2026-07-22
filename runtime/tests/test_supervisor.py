"""Unit tests for the supervisor sweep — no database.

`sweep` is pure orchestration over three injected callables (find-stale,
re-kick, force-fail), so we exercise its routing (re-kick vs exhausted-fail)
with in-memory fakes and a sentinel connection. No DB, no hang.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from runtime.models import Task, TaskStatus
from runtime.supervisor import sweep

CONN = object()  # opaque sentinel; the fakes ignore it


def _task(retries: int = 0, status: TaskStatus = TaskStatus.IN_PROGRESS) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(),
        workstream="ws",
        type="t",
        status=status,
        priority=0,
        retries=retries,
        created_at=now,
        updated_at=now,
    )


class _Recorder:
    """Fake re-kick / fail callable that records the tasks it was handed."""

    def __init__(self, returns_none: bool = False):
        self.calls: list[Task] = []
        self.returns_none = returns_none

    def rekick(self, conn, task):
        assert conn is CONN
        self.calls.append(task)
        return None if self.returns_none else task

    def fail(self, conn, task, max_retries):
        assert conn is CONN
        self.max_retries = max_retries
        self.calls.append(task)
        return None if self.returns_none else task


def test_sweep_rekicks_tasks_below_max():
    below = [_task(retries=0), _task(retries=2)]
    rk, fl = _Recorder(), _Recorder()
    res = sweep(
        CONN, threshold_s=60, max_retries=3,
        find_stale=lambda c, t: below, rekick=rk.rekick, fail_exhausted=fl.fail,
    )
    assert res.scanned == 2
    assert set(res.rekicked) == {t.id for t in below}
    assert res.failed == []
    assert [t.id for t in rk.calls] == [t.id for t in below]
    assert fl.calls == []


def test_sweep_force_fails_at_or_above_max():
    at_max, over = _task(retries=3), _task(retries=5)
    rk, fl = _Recorder(), _Recorder()
    res = sweep(
        CONN, threshold_s=60, max_retries=3,
        find_stale=lambda c, t: [at_max, over], rekick=rk.rekick, fail_exhausted=fl.fail,
    )
    assert set(res.failed) == {at_max.id, over.id}
    assert res.rekicked == []
    assert rk.calls == []
    assert fl.max_retries == 3


def test_sweep_mixed_batch_routes_each_task():
    keep, drop = _task(retries=1), _task(retries=4)
    rk, fl = _Recorder(), _Recorder()
    res = sweep(
        CONN, threshold_s=60, max_retries=4,
        find_stale=lambda c, t: [keep, drop], rekick=rk.rekick, fail_exhausted=fl.fail,
    )
    assert res.rekicked == [keep.id]   # retries 1 < 4 → re-kick
    assert res.failed == [drop.id]     # retries 4 >= 4 → fail
    assert res.acted is True


def test_sweep_empty_when_nothing_stale():
    res = sweep(CONN, threshold_s=60, max_retries=3, find_stale=lambda c, t: [])
    assert res.scanned == 0 and not res.acted


def test_sweep_skips_when_action_returns_none():
    # A task that changed state between scan and write → helper returns None,
    # so it is counted as scanned but not recorded as acted-upon.
    rk = _Recorder(returns_none=True)
    res = sweep(
        CONN, threshold_s=60, max_retries=3,
        find_stale=lambda c, t: [_task(retries=0)], rekick=rk.rekick,
    )
    assert res.scanned == 1 and res.rekicked == [] and res.failed == []


def test_sweep_one_bad_task_does_not_sink_the_rest():
    good, bad = _task(retries=0), _task(retries=0)

    def flaky(conn, task):
        if task.id == bad.id:
            raise RuntimeError("boom")
        return task

    res = sweep(
        CONN, threshold_s=60, max_retries=3,
        find_stale=lambda c, t: [bad, good], rekick=flaky,
    )
    assert res.scanned == 2
    assert res.rekicked == [good.id]  # good still handled despite bad raising
