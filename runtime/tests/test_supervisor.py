"""Unit tests for the supervisor sweep — the graduated recovery ladder (ADR-0023).

`sweep` is pure orchestration over injected callables (find-stale, nudge, re-kick,
escalate-stuck, force-fail), so we exercise its rung routing (nudge → defer →
escalate-stuck → abandon → re-kick) with in-memory fakes and a sentinel connection.
No DB, no hang. The DB-backed mechanics live in test_integration_db / test_concurrency_db.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from runtime.models import Task, TaskStatus
from runtime.supervisor import sweep

CONN = object()  # opaque sentinel; the fakes ignore it
NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _task(
    *,
    retries: int = 0,
    no_progress_rekicks: int = 0,
    nudged_at: datetime | None = None,
    status: TaskStatus = TaskStatus.IN_PROGRESS,
) -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(),
        workstream="ws",
        type="t",
        status=status,
        priority=0,
        retries=retries,
        no_progress_rekicks=no_progress_rekicks,
        nudged_at=nudged_at,
        created_at=now,
        updated_at=now,
    )


class _Recorder:
    """Fake ladder callable that records the tasks it was handed (and returns them,
    or None to model a scan→write race where the task changed state)."""

    def __init__(self, returns_none: bool = False):
        self.calls: list[Task] = []
        self.returns_none = returns_none

    def nudge(self, conn, task, grace_s):
        assert conn is CONN
        self.grace_s = grace_s
        self.calls.append(task)
        return None if self.returns_none else task

    def rekick(self, conn, task):
        assert conn is CONN
        self.calls.append(task)
        return None if self.returns_none else task

    def escalate(self, conn, task):
        assert conn is CONN
        self.calls.append(task)
        return None if self.returns_none else task

    def fail(self, conn, task, max_retries):
        assert conn is CONN
        self.max_retries = max_retries
        self.calls.append(task)
        return None if self.returns_none else task


def _sweep(tasks, **kw):
    """Run one sweep over `tasks` with fresh recorders; return (result, recorders)."""
    nd, rk, es, fl = _Recorder(), _Recorder(), _Recorder(), _Recorder()
    kw.setdefault("now", NOW)
    res = sweep(
        CONN, threshold_s=60, max_retries=kw.pop("max_retries", 5),
        find_stale=lambda c, t: tasks,
        nudge=nd.nudge, rekick=rk.rekick, escalate_stuck=es.escalate,
        fail_exhausted=fl.fail, **kw,
    )
    return res, (nd, rk, es, fl)


# --- Rung 1: nudge on first detection ---------------------------------------


def test_sweep_nudges_on_first_detection():
    """A freshly-stale task (no open nudge episode) is NUDGED, not re-kicked."""
    t = _task(nudged_at=None)
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45)
    assert res.nudged == [t.id]
    assert res.rekicked == [] and res.stuck == [] and res.failed == []
    assert [x.id for x in nd.calls] == [t.id] and nd.grace_s == 45
    assert rk.calls == [] and es.calls == [] and fl.calls == []


def test_sweep_nudge_skipped_when_disabled_goes_straight_to_rekick():
    """nudge_grace_s=0 disables the nudge rung (pre-ADR-0023 timing): re-kick now."""
    t = _task(nudged_at=None)
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=0)
    assert res.nudged == [] and res.rekicked == [t.id]
    assert nd.calls == []


# --- Rung 2: defer within the grace window ----------------------------------


def test_sweep_defers_while_within_nudge_grace():
    """Already nudged + still inside the grace window → NO action (awaiting recovery)."""
    t = _task(nudged_at=NOW - timedelta(seconds=10))  # 10s < 45s grace
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45)
    assert res.deferred == [t.id]
    assert not res.acted  # nudged/rekicked/stuck/failed all empty
    assert nd.calls == [] and rk.calls == [] and es.calls == [] and fl.calls == []


def test_sweep_rekicks_after_grace_elapses():
    """Nudged + grace elapsed + still stale → re-kick (the worker never recovered)."""
    t = _task(nudged_at=NOW - timedelta(seconds=60))  # 60s > 45s grace
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45)
    assert res.rekicked == [t.id]
    assert [x.id for x in rk.calls] == [t.id]
    assert nd.calls == [] and es.calls == [] and fl.calls == []


# --- Rung 3: escalate-to-PM (stuck) EARLY, before max_retries ---------------


def test_sweep_escalates_stuck_when_no_progress_threshold_reached():
    """no_progress_rekicks >= threshold → escalate (task.stuck) + supersede, NOT
    re-kick — and it exits EARLY (retries still well below max_retries)."""
    t = _task(retries=2, no_progress_rekicks=2, nudged_at=NOW - timedelta(seconds=60))
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45, max_retries=5, stuck_threshold=2)
    assert res.stuck == [t.id]
    assert res.rekicked == [] and res.failed == []      # did NOT keep re-kicking
    assert [x.id for x in es.calls] == [t.id]
    assert rk.calls == [] and fl.calls == []
    assert t.retries < 5  # bailed to re-decomposition BEFORE exhausting retries


def test_sweep_stuck_takes_precedence_over_max_retries():
    """When BOTH the stuck threshold and max_retries are met, the early stuck
    bail-out wins (we stop re-kicking a no-progress task rather than abandon it)."""
    t = _task(retries=5, no_progress_rekicks=3, nudged_at=NOW - timedelta(seconds=60))
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45, max_retries=5, stuck_threshold=2)
    assert res.stuck == [t.id] and res.failed == []


# --- Rung 4: abandon backstop for a progressing-but-slow task ---------------


def test_sweep_force_fails_at_max_when_still_making_progress():
    """A task that keeps making progress (no_progress_rekicks below threshold) but
    never finishes hits the max_retries force-abandon backstop, NOT the stuck path."""
    t = _task(retries=5, no_progress_rekicks=0, nudged_at=NOW - timedelta(seconds=60))
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45, max_retries=5, stuck_threshold=2)
    assert res.failed == [t.id]
    assert res.stuck == [] and res.rekicked == []
    assert fl.max_retries == 5


# --- Rung 5 + robustness ----------------------------------------------------


def test_sweep_rekicks_below_thresholds_after_grace():
    """Grace elapsed, below both stuck and max thresholds → ordinary re-kick."""
    t = _task(retries=1, no_progress_rekicks=1, nudged_at=NOW - timedelta(seconds=60))
    res, (nd, rk, es, fl) = _sweep([t], nudge_grace_s=45, max_retries=5, stuck_threshold=2)
    assert res.rekicked == [t.id]


def test_sweep_empty_when_nothing_stale():
    res, _ = _sweep([], nudge_grace_s=45)
    assert res.scanned == 0 and not res.acted


def test_sweep_skips_when_nudge_returns_none():
    """A task that recovered between scan and write → nudge returns None; counted as
    scanned but not acted-upon (no clobber)."""
    t = _task(nudged_at=None)
    nd = _Recorder(returns_none=True)
    res = sweep(
        CONN, threshold_s=60, max_retries=5, nudge_grace_s=45, now=NOW,
        find_stale=lambda c, s: [t], nudge=nd.nudge,
    )
    assert res.scanned == 1 and not res.acted


def test_sweep_one_bad_task_does_not_sink_the_rest():
    """One rung raising must not abort the others in the batch (grace elapsed so both
    reach the re-kick rung)."""
    good = _task(nudged_at=NOW - timedelta(seconds=60))
    bad = _task(nudged_at=NOW - timedelta(seconds=60))

    def flaky(conn, task):
        if task.id == bad.id:
            raise RuntimeError("boom")
        return task

    res = sweep(
        CONN, threshold_s=60, max_retries=5, nudge_grace_s=45, now=NOW,
        find_stale=lambda c, t: [bad, good], rekick=flaky,
    )
    assert res.scanned == 2
    assert res.rekicked == [good.id]  # good still handled despite bad raising
