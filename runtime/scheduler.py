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
from .models import Task
from .tasks import enqueue_task

log = logging.getLogger("runtime.scheduler")

# The canonical PM-pulse task type + its default cadence.
PM_TICK_TYPE = "pm.tick"
DEFAULT_PULSE_INTERVAL_S = 300.0

# Injectable seams so `tick_once` is unit-testable with no database.
PendingCheck = Callable[[psycopg.Connection, str], bool]
Enqueuer = Callable[..., Task]


def _pm_tick_pending(conn: psycopg.Connection, workstream: str) -> bool:
    """True if a ``pm.tick`` for ``workstream`` is already queued or in progress."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM tasks
            WHERE workstream = %s
              AND type = %s
              AND status IN ('queued', 'in_progress')
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
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            tick_once(conn, workstream)
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
