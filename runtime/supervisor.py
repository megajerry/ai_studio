"""The non-agent supervisor — the irreducible liveness guarantee (ADR-0004).

A dumb, always-on, **non-LLM** loop whose only job is *"no task is ever silently
dropped."* It scans for in-progress tasks whose heartbeat has gone stale (a
worker that crashed / hallucinated / ran out of budget before finishing) and
**re-kicks** them: reset to ``up_for_grabs``, clear the claim, bump a retry
counter, emit ``task.rekicked``. Once a task exhausts its retries it is
force-abandoned (``task.failed_exhausted``) rather than churning forever.

There are **no model calls here** — this layer exists precisely to provide the
reliability an agent cannot provide for itself (the crash-before-checkpoint gap).
It must be simple, well-tested, and kept alive by the OS (launchd ``KeepAlive``),
because it is the one thing that must not itself silently die.

Run it as a singleton::

    python -m runtime.supervisor

Config (env, with sane defaults):

===========================  =======  =====================================
Env var                      Default  Meaning
===========================  =======  =====================================
``SUPERVISOR_INTERVAL_S``    30       Seconds between sweeps.
``SUPERVISOR_STALE_S``       120      Heartbeat age past which a task is stale.
``SUPERVISOR_MAX_RETRIES``   5        Re-kicks before force-failing a task.
===========================  =======  =====================================
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import UUID

import psycopg

from .db import connect
from .events import append_event
from .models import EventType, Task, TaskStatus, make_event
from .tasks import complete_task, find_stale_tasks, rekick_task

log = logging.getLogger("runtime.supervisor")

# --- Defaults (overridable via env) -----------------------------------------
DEFAULT_INTERVAL_S = 30.0
DEFAULT_STALE_S = 120.0
DEFAULT_MAX_RETRIES = 5


# --- Injectable seams (defaults are the real DB ops; tests pass fakes) ------
# `sweep` is pure orchestration over these three callables, so it is unit
# testable with an in-memory task source and no database.
StaleFinder = Callable[[psycopg.Connection, float], "list[Task]"]
Rekicker = Callable[[psycopg.Connection, Task], Optional[Task]]
Failer = Callable[[psycopg.Connection, Task, int], Optional[Task]]


def _default_rekick(conn: psycopg.Connection, task: Task) -> Optional[Task]:
    return rekick_task(conn, task.id)


def _default_fail_exhausted(
    conn: psycopg.Connection, task: Task, max_retries: int
) -> Optional[Task]:
    """Force-fail a task that has exhausted its re-kicks; emit the audit event.

    Guarded to ``in_progress``: the automatic exhausted-fail must NOT clobber a
    task that self-completed (reached ``done``/``failed``) in the scan→write
    window (``find_stale_tasks`` → here). Like :func:`runtime.tasks.rekick_task`,
    we finalize only a task still ``in_progress``; if it already reached a
    terminal state ``complete_task`` (unforced) leaves it untouched and returns
    ``None``, and this returns ``None`` (the sweep logs a skip). ``force`` on
    :func:`complete_task` stays available for genuine manual use.

    On a real fail, ``complete_task`` finalizes the row and emits
    ``task.finished``; we additionally emit ``task.failed_exhausted`` so the
    reason is traceable in the event log. Both run in one transaction (each
    nests as a savepoint) so the fail and its audit event commit atomically.
    """
    with conn.transaction():
        failed = complete_task(
            conn,
            task.id,
            status=TaskStatus.ABANDONED,
            result={
                "reason": "max_retries_exhausted",
                "retries": task.retries,
                "max_retries": max_retries,
            },
        )
        if failed is not None:
            append_event(
                conn,
                make_event(
                    workstream=task.workstream,
                    type=EventType.TASK_FAILED_EXHAUSTED.value,
                    task_id=task.id,
                    payload={
                        "status": TaskStatus.ABANDONED.value,
                        "retries": task.retries,
                        "max_retries": max_retries,
                    },
                ),
            )
    return failed


@dataclass
class SweepResult:
    """What a single sweep did — returned for logging/telemetry and tests."""

    scanned: int = 0
    rekicked: list[UUID] = field(default_factory=list)
    failed: list[UUID] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.rekicked or self.failed)


def sweep(
    conn: psycopg.Connection,
    threshold_s: float,
    max_retries: int,
    *,
    find_stale: StaleFinder = find_stale_tasks,
    rekick: Rekicker = _default_rekick,
    fail_exhausted: Failer = _default_fail_exhausted,
) -> SweepResult:
    """Run one supervisor pass (single, testable unit).

    Finds stale in-progress tasks and, for each: force-fails it if it has
    already been re-kicked ``max_retries`` times, otherwise re-kicks it. Every
    action emits an event (``task.rekicked`` / ``task.failed_exhausted``), so
    the liveness layer is itself fully traceable. Each task is handled by a
    transactional helper, so a failure on one task does not abort the others.
    """
    result = SweepResult()
    for task in find_stale(conn, threshold_s):
        result.scanned += 1
        try:
            if task.retries >= max_retries:
                if fail_exhausted(conn, task, max_retries) is not None:
                    result.failed.append(task.id)
                    log.warning(
                        "force-failed task %s after %d retries (>= max %d)",
                        task.id,
                        task.retries,
                        max_retries,
                    )
                else:
                    # Guarded no-op: the task self-completed in the scan→write
                    # window, so we must not clobber its terminal state.
                    log.info(
                        "skipped exhausted-fail of task %s: no longer in_progress "
                        "(self-completed before sweep)",
                        task.id,
                    )
            else:
                if rekick(conn, task) is not None:
                    result.rekicked.append(task.id)
                    log.info(
                        "re-kicked stale task %s (retry %d/%d)",
                        task.id,
                        task.retries + 1,
                        max_retries,
                    )
        except Exception:  # one bad task must not sink the whole sweep
            log.exception("supervisor failed to handle task %s", task.id)
    return result


# --- Config + loop ----------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def run(
    interval_s: Optional[float] = None,
    threshold_s: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> None:
    """Loop forever, sweeping every ``interval_s``. Thin — delegates to :func:`sweep`.

    Config falls back to env (then module defaults). The loop reconnects on a
    dropped/failed connection and never lets one bad sweep kill the process:
    the supervisor is the guarantee of last resort, so it must keep running.
    launchd ``KeepAlive`` restarts it if the process itself dies.
    """
    interval_s = (
        _env_float("SUPERVISOR_INTERVAL_S", DEFAULT_INTERVAL_S)
        if interval_s is None
        else interval_s
    )
    threshold_s = (
        _env_float("SUPERVISOR_STALE_S", DEFAULT_STALE_S)
        if threshold_s is None
        else threshold_s
    )
    max_retries = (
        _env_int("SUPERVISOR_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        if max_retries is None
        else max_retries
    )

    log.info(
        "supervisor starting: interval=%.0fs stale=%.0fs max_retries=%d",
        interval_s,
        threshold_s,
        max_retries,
    )
    conn: Optional[psycopg.Connection] = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            result = sweep(conn, threshold_s, max_retries)
            if result.acted:
                log.info(
                    "sweep: scanned=%d rekicked=%d failed=%d",
                    result.scanned,
                    len(result.rekicked),
                    len(result.failed),
                )
        except Exception:
            log.exception("supervisor sweep failed; will retry after interval")
            conn = _safe_close(conn)
        time.sleep(interval_s)


def _safe_close(conn: Optional[psycopg.Connection]) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
