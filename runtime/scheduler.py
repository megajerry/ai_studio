"""The scheduler — ensures the Productivity PM pulse (ADR-0009).

The PM is **not** a daemon; it wakes on a cadence, does bounded work, and exits
(ADR-0009, [bootstrap-sequence Layer 2](../docs/bootstrap-sequence.md)). This
scheduler is the cadence: every ``PULSE_INTERVAL_S`` it enqueues a ``pm.tick``
task — "spawning the PM" = enqueuing a task — which the runtime materializes a PM
worker to service.

Like the supervisor this is a **non-LLM** singleton. To avoid a pileup when the
PM is slow or wedged, it enqueues a ``pm.tick`` **only if** none is already
``queued`` or ``in_progress``; a stuck one is the supervisor's problem, not the
scheduler's. Run it as::

    python -m runtime.scheduler

Config: ``PULSE_INTERVAL_S`` (default 300s = 5 min).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import psycopg

from .db import connect
from .event_types import EVENT_TASK_STUCK
from .events import DEFAULT_LOOKBACK, read_events
from .models import Event, Task
from .roles.pm import REPLAN_TASK_TYPE
from .tasks import enqueue_task

log = logging.getLogger("runtime.scheduler")

# The canonical PM-pulse task type + its default cadence.
PM_TICK_TYPE = "pm.tick"
DEFAULT_PULSE_INTERVAL_S = 300.0

#: Max ``task.stuck`` events consumed per replan-dispatch pass (bounded work).
REPLAN_DISPATCH_LIMIT = 200

# Injectable seams so `tick_once` is unit-testable with no database.
PendingCheck = Callable[[psycopg.Connection, str], bool]
Enqueuer = Callable[..., Task]
EventReader = Callable[..., list]
ReplanExistsCheck = Callable[[psycopg.Connection, object], bool]


def _pm_tick_pending(conn: psycopg.Connection, workstream: str) -> bool:
    """True if a ``pm.tick`` for ``workstream`` is already queued or in progress."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM tasks
            WHERE workstream = %s
              AND type = %s
              AND status NOT IN ('merged', 'abandoned')
            LIMIT 1
            """,
            (workstream, PM_TICK_TYPE),
        )
        found = cur.fetchone() is not None
    # Don't leave a long-lived connection idle-in-transaction after a read.
    if not conn.autocommit:
        conn.commit()
    return found


def tick_once(
    conn: psycopg.Connection,
    workstream: str = "productivity",
    *,
    pending: PendingCheck = _pm_tick_pending,
    enqueue: Enqueuer = enqueue_task,
) -> Optional[Task]:
    """Ensure exactly one live PM pulse (single, testable unit).

    Enqueues a ``pm.tick`` task and returns it, **unless** one is already
    ``queued``/``in_progress`` — in which case it returns ``None`` (skip, avoid
    pileup). The enqueue emits ``task.created`` (via :func:`enqueue_task`), so
    the pulse is traceable in the event log.
    """
    if pending(conn, workstream):
        log.debug("pm.tick already pending for %s; skipping", workstream)
        return None
    task = enqueue(conn, workstream=workstream, type=PM_TICK_TYPE, payload={"kind": "pulse"})
    log.info("enqueued pm.tick %s for %s", task.id, workstream)
    return task


# --- Replan dispatch: task.stuck signal → PM replan task (ADR-0023, R2) ------
#
# The queue consumer that turns the supervisor's ``task.stuck`` SIGNAL into work
# WITHOUT any agent-to-agent call (CLAUDE.md invariant 1): it scans NEW
# ``task.stuck`` events past a ``seq`` cursor and enqueues ONE ``replan`` task per
# stuck task, which the worker dispatches to :func:`runtime.roles.pm.run_pm_replan`.
# Idempotent: a stuck task that already has a replan task is skipped, so a cursor
# reset (process restart) never double-enqueues.
#
# COMMIT-SAFETY: ``events.seq`` is insert-order, not commit-order (see
# runtime/events.py), so a ``task.stuck`` that commits *after* the cursor advanced
# past its seq would be lost to a plain ``since_seq`` read — a stuck task that
# never gets re-decomposed. We read with a bounded ``lookback`` overlap so such a
# late-committed signal is re-observed on a subsequent pass, and the existing
# ``_has_replan_task`` idempotency drops the duplicate. This recovers any stuck
# signal whose transaction commits within ``lookback`` seqs of the cursor.


def _has_replan_task(conn: psycopg.Connection, stuck_task_id: object) -> bool:
    """True if a ``replan`` task already exists for ``stuck_task_id`` (idempotency)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM tasks WHERE type = %s AND payload->>'stuck_task_id' = %s LIMIT 1",
            (REPLAN_TASK_TYPE, str(stuck_task_id)),
        )
        found = cur.fetchone() is not None
    if not conn.autocommit:
        conn.commit()
    return found


def dispatch_replans(
    conn: psycopg.Connection,
    *,
    since_seq: int = 0,
    lookback: int = 0,
    limit: int = REPLAN_DISPATCH_LIMIT,
    read: EventReader = read_events,
    has_replan: ReplanExistsCheck = _has_replan_task,
    enqueue: Enqueuer = enqueue_task,
) -> tuple[int, list[str]]:
    """Enqueue one PM ``replan`` task per NEW ``task.stuck`` event (queue-only).

    Scans ``task.stuck`` events with a bounded ``lookback`` overlap behind
    ``since_seq`` (effective lower bound ``since_seq - lookback``, ascending,
    bounded by ``limit`` — which applies *after* the ``task.stuck`` type filter, so
    the sparse re-scan is cheap) and, for each stuck task that does not already have
    a replan, enqueues a ``replan`` task in the stuck task's workstream carrying its
    ``stuck_task_id``. The overlap makes this **commit-safe**: a ``task.stuck`` that
    commits out-of-order (after the cursor advanced past its seq) is re-observed and
    dispatched rather than lost — ``_has_replan_task`` idempotency drops any
    duplicate re-scan.

    ``lookback`` defaults to ``0`` (the classic exclusive ``seq > since_seq`` read);
    the long-running :func:`run` loop passes :data:`~runtime.events.DEFAULT_LOOKBACK`
    to get commit-safety in production.

    Returns ``(new_cursor, [replan_task_ids])`` — the new cursor is the high-water
    ``seq`` scanned, never below the incoming ``since_seq`` (the overlap re-scans
    lower seqs but never drags the cursor backwards). Every seam is injectable so
    the consumer is unit-testable with no database.
    """
    events: list[Event] = read(
        conn, type=EVENT_TASK_STUCK, since_seq=since_seq, lookback=lookback, limit=limit
    )
    cursor = since_seq  # the overlap re-scans lower seqs but never rewinds the cursor
    replan_ids: list[str] = []
    for ev in events:
        cursor = max(cursor, ev.seq or 0)
        if ev.task_id is None:
            continue
        if has_replan(conn, ev.task_id):
            continue  # idempotent: this stuck task already has a replan
        task = enqueue(
            conn,
            workstream=ev.workstream,
            type=REPLAN_TASK_TYPE,
            payload={"stuck_task_id": str(ev.task_id)},
        )
        replan_ids.append(str(task.id))
        log.info("enqueued replan %s for stuck task %s", task.id, ev.task_id)
    return cursor, replan_ids


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def run(
    interval_s: Optional[float] = None, workstream: str = "productivity"
) -> None:
    """Loop forever, ticking every ``interval_s``. Thin — delegates to :func:`tick_once`.

    Reconnects on a dropped connection and swallows per-tick errors so a
    transient DB blip can't stop the pulse. launchd ``KeepAlive`` restarts the
    process if it dies.
    """
    interval_s = (
        _env_float("PULSE_INTERVAL_S", DEFAULT_PULSE_INTERVAL_S)
        if interval_s is None
        else interval_s
    )
    log.info("scheduler starting: pm.tick every %.0fs for %s", interval_s, workstream)
    conn: Optional[psycopg.Connection] = None
    # In-memory high-water cursor over task.stuck events (ADR-0023, R2). Starts at 0
    # (a fresh process re-scans, but dispatch is idempotent so no duplicate replans);
    # advances each pass so steady-state scans only new events.
    replan_cursor = 0
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            tick_once(conn, workstream)
            # Turn any new task.stuck signal into a PM replan task (queue-only).
            # Commit-safe: a bounded lookback overlap re-observes a task.stuck that
            # committed out-of-order (below the cursor), and _has_replan_task drops
            # the duplicate — so a late-committed stall signal is never lost.
            replan_cursor, _ = dispatch_replans(
                conn, since_seq=replan_cursor, lookback=DEFAULT_LOOKBACK
            )
        except Exception:
            log.exception("scheduler tick failed; will retry after interval")
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
