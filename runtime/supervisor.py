"""The non-agent supervisor — the irreducible liveness guarantee (ADR-0004).

A dumb, always-on, **non-LLM** loop whose only job is *"no task is ever silently
dropped."* It scans for in-progress tasks whose heartbeat has gone stale (a
worker that crashed / hallucinated / ran out of budget before finishing) and
recovers them along a graduated, progress-aware **ladder** (ADR-0023), cheapest
rung first:

    nudge + grace → re-kick → (no net progress) escalate-to-PM → abandon

1. **Nudge + grace** (``task.nudge``) — on the FIRST detection of a stall, mark
   ``nudged_at`` and DEFER the re-kick for a short grace window WITHOUT touching
   the claim, so a transient stall (a slow tool, an API hiccup) can recover with
   its in-flight progress **preserved**. A heartbeat within the grace clears the
   episode (no reset); a truly dead process just falls through to the next rung.
2. **Re-kick** (``task.rekicked``) — still stale after the grace → reset to
   ``up_for_grabs``, clear the claim, bump ``retries``, re-run from scratch (the
   pre-ADR-0023 move). Each re-kick measures NET progress since the last attempt
   (:func:`runtime.tasks.task_made_progress`): progress resets ``no_progress_rekicks``
   to 0, no progress increments it.
3. **Escalate-to-PM** (``task.stuck``) — once ``no_progress_rekicks`` reaches the
   stuck threshold (default 2, **less** than max_retries), STOP re-kicking: emit
   the ``task.stuck`` SIGNAL and supersede the attempt (abandoned,
   ``reason=stuck_needs_replan``) so R2's PM can re-decompose it into smaller
   subtasks. This bails out EARLY instead of burning every retry making zero
   progress. (This layer emits ONLY the signal — it does NOT enqueue the PM task.)
4. **Abandon** (``task.failed_exhausted``) — the unchanged backstop for a task
   that DOES make progress but never finishes: at ``SUPERVISOR_MAX_RETRIES`` it is
   force-abandoned rather than churning forever.

There are **no model calls here** — this layer exists precisely to provide the
reliability an agent cannot provide for itself (the crash-before-checkpoint gap).
It must be simple, well-tested, and kept alive by the OS (launchd ``KeepAlive``),
because it is the one thing that must not itself silently die.

Run it as a singleton::

    python -m runtime.supervisor

Config (env, with sane defaults):

=================================  =======  ===============================================
Env var                            Default  Meaning
=================================  =======  ===============================================
``SUPERVISOR_INTERVAL_S``          30       Seconds between sweeps.
``SUPERVISOR_STALE_S``             120      Heartbeat age past which a task is stale.
``SUPERVISOR_MAX_RETRIES``         5        Re-kicks before force-abandoning a task.
``SUPERVISOR_NUDGE_GRACE_S``       45       After nudging a stall, defer the re-kick this
                                            long so a transient stall recovers with progress
                                            preserved (0 disables the nudge rung).
``SUPERVISOR_STUCK_THRESHOLD``     2        No-progress re-kicks before escalating to the PM
                                            (``task.stuck``) instead of re-kicking; kept
                                            below max_retries so we bail EARLY.
``SUPERVISOR_RECONNECT_GRACE_S``   60       After recovering from a DB outage, defer
                                            re-kicks this long so live workers re-heartbeat
                                            first (anti thundering-herd; see ADR-0017).
=================================  =======  ===============================================

DB-outage resilience (ADR-0017)
-------------------------------
The store may be a remote host and thus unreachable for stretches. Two guards keep
the supervisor correct across an outage:

1. **Degraded connect** — the loop opens its connection through
   :func:`runtime.db.connect_with_retry`, so an unreachable DB surfaces as a
   single :class:`runtime.db.DBUnavailable` signal (log degraded + retry next
   interval), never a crash or a hang.
2. **Reconnect grace window** — during an outage no worker can write a heartbeat,
   so on recovery *every* in-progress task looks stale at once. Re-kicking them
   immediately would be a thundering-herd stampede against live workers. After a
   reconnect that follows an outage, the supervisor defers its sweep for
   ``SUPERVISOR_RECONNECT_GRACE_S`` so live workers re-heartbeat first; only tasks
   still stale after the window are re-kicked.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from uuid import UUID

import psycopg

from .db import DBUnavailable, connect_with_retry
from .events import append_event
from .models import EventType, Task, TaskStatus, make_event
from .tasks import (
    complete_task,
    escalate_stuck_task,
    find_stale_tasks,
    nudge_task,
    rekick_task,
    task_made_progress,
)

log = logging.getLogger("runtime.supervisor")

# --- Defaults (overridable via env) -----------------------------------------
DEFAULT_INTERVAL_S = 30.0
DEFAULT_STALE_S = 120.0
DEFAULT_MAX_RETRIES = 5
#: After nudging a stall, defer the re-kick this long so a transient stall can
#: recover with its in-flight progress preserved (ADR-0023). 0 disables the nudge
#: rung (the sweep re-kicks / escalates on first detection, pre-ADR-0023 timing).
DEFAULT_NUDGE_GRACE_S = 45.0
#: Consecutive no-progress re-kicks before escalating to the PM (``task.stuck``)
#: instead of re-kicking. Kept BELOW max_retries so an endless-reset loop bails to
#: re-decomposition EARLY (ADR-0023).
DEFAULT_STUCK_THRESHOLD = 2
#: After recovering from a DB outage, defer re-kicks this long so live workers
#: re-heartbeat first (anti thundering-herd, ADR-0017).
DEFAULT_RECONNECT_GRACE_S = 60.0


# --- Injectable seams (defaults are the real DB ops; tests pass fakes) ------
# `sweep` is pure orchestration over these callables, so it is unit testable with
# an in-memory task source and no database.
StaleFinder = Callable[[psycopg.Connection, float], "list[Task]"]
Nudger = Callable[[psycopg.Connection, Task, float], Optional[Task]]
Rekicker = Callable[[psycopg.Connection, Task], Optional[Task]]
Escalator = Callable[[psycopg.Connection, Task], Optional[Task]]
Failer = Callable[[psycopg.Connection, Task, int], Optional[Task]]


def _default_nudge(
    conn: psycopg.Connection, task: Task, grace_s: float
) -> Optional[Task]:
    return nudge_task(conn, task.id, grace_s=grace_s)


def _default_rekick(conn: psycopg.Connection, task: Task) -> Optional[Task]:
    # Progress-aware (ADR-0023): measure NET progress since the last attempt, then
    # re-kick maintaining ``no_progress_rekicks`` accordingly (reset on progress,
    # increment on none).
    made_progress = task_made_progress(conn, task.id)
    return rekick_task(conn, task.id, made_progress=made_progress)


def _default_escalate_stuck(conn: psycopg.Connection, task: Task) -> Optional[Task]:
    """Emit the ``task.stuck`` signal + supersede the attempt for PM re-decomposition.

    Guarded exactly like :func:`_default_fail_exhausted`: the abandon is NOT forced,
    so a task that self-completed in the scan→write window is left untouched and this
    returns ``None`` (the sweep logs a skip).
    """
    return escalate_stuck_task(
        conn,
        task.id,
        stall_reason=task.stall_reason or "no_progress",
        no_progress_rekicks=task.no_progress_rekicks,
        retries=task.retries,
    )


def _nudge_grace_elapsed(task: Task, now: datetime, grace_s: float) -> bool:
    """True once a nudged task's grace window has elapsed (pure; ADR-0023).

    A task with no open nudge episode (``nudged_at is None``) has nothing to wait on
    (returns True). Compares ``now`` against ``nudged_at + grace_s`` using the same
    tz-normalization as :func:`runtime.models.is_stale`, so it is deterministic under
    an injected clock and never crashes on a naive DB timestamp.
    """
    if task.nudged_at is None:
        return True
    nudged = task.nudged_at
    if nudged.tzinfo is None:
        nudged = nudged.replace(tzinfo=timezone.utc)
    return (now - nudged).total_seconds() >= grace_s


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
    nudged: list[UUID] = field(default_factory=list)
    #: Stale + nudged but still inside the grace window → no action this pass.
    deferred: list[UUID] = field(default_factory=list)
    rekicked: list[UUID] = field(default_factory=list)
    #: Escalated to the PM (``task.stuck``) + superseded (no-progress bail-out).
    stuck: list[UUID] = field(default_factory=list)
    failed: list[UUID] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.nudged or self.rekicked or self.stuck or self.failed)


def sweep(
    conn: psycopg.Connection,
    threshold_s: float,
    max_retries: int,
    *,
    nudge_grace_s: float = DEFAULT_NUDGE_GRACE_S,
    stuck_threshold: int = DEFAULT_STUCK_THRESHOLD,
    find_stale: StaleFinder = find_stale_tasks,
    nudge: Nudger = _default_nudge,
    rekick: Rekicker = _default_rekick,
    escalate_stuck: Escalator = _default_escalate_stuck,
    fail_exhausted: Failer = _default_fail_exhausted,
    now: Optional[datetime] = None,
) -> SweepResult:
    """Run one supervisor pass over the graduated recovery ladder (ADR-0023).

    Finds stale held tasks and, for each, climbs the cheapest applicable rung:

    1. **nudge** — first detection this stall episode (``nudged_at is None`` and
       ``nudge_grace_s > 0``): mark it + defer, giving the worker a grace window to
       recover with progress preserved (``task.nudge``).
    2. **defer** — already nudged but still inside the grace window: no action.
    3. **escalate-to-PM** — grace elapsed, still stale, and ``no_progress_rekicks
       >= stuck_threshold``: STOP re-kicking, emit ``task.stuck`` + supersede
       (bail EARLY to re-decomposition, before max_retries).
    4. **abandon** — grace elapsed, still stale, and ``retries >= max_retries``:
       the force-abandon backstop for a progressing-but-never-finishing task.
    5. **re-kick** — grace elapsed, still stale, otherwise: reset to the grab pool
       (``task.rekicked``), measuring progress to maintain ``no_progress_rekicks``.

    The stuck check precedes the max_retries check because the stuck threshold is
    intentionally the lower bound (bail early on no progress). Every action emits an
    event, so the liveness layer stays fully traceable, and each task is handled by a
    transactional helper so one bad task never aborts the others. A helper returning
    ``None`` (the task changed state in the scan→write window) is a logged skip, never
    a clobber. With ``nudge_grace_s <= 0`` the nudge rung is skipped entirely
    (pre-ADR-0023 timing: re-kick / escalate on first detection).
    """
    now = datetime.now(timezone.utc) if now is None else now
    result = SweepResult()
    for task in find_stale(conn, threshold_s):
        result.scanned += 1
        try:
            # Rung 1 — nudge (cheapest): first detection of this stall episode.
            if nudge_grace_s > 0 and task.nudged_at is None:
                if nudge(conn, task, nudge_grace_s) is not None:
                    result.nudged.append(task.id)
                    log.info(
                        "nudged stale task %s; deferring re-kick %.0fs for recovery",
                        task.id, nudge_grace_s,
                    )
                else:
                    log.info(
                        "skipped nudge of task %s: no longer stale/held "
                        "(recovered before sweep)", task.id,
                    )
                continue

            # Rung 2 — defer: nudged, still stale, but inside the grace window.
            if nudge_grace_s > 0 and not _nudge_grace_elapsed(task, now, nudge_grace_s):
                result.deferred.append(task.id)
                log.info(
                    "task %s still within nudge grace; awaiting recovery", task.id
                )
                continue

            # Grace elapsed (or nudge disabled) and STILL stale → climb the ladder.
            if task.no_progress_rekicks >= stuck_threshold:
                # Rung 3 — escalate to PM: no net progress across re-kicks. Bail
                # EARLY (before max_retries) to re-decomposition; STOP re-kicking.
                if escalate_stuck(conn, task) is not None:
                    result.stuck.append(task.id)
                    log.warning(
                        "escalated stuck task %s to PM after %d no-progress re-kick(s) "
                        "(threshold %d) — superseded for re-decomposition",
                        task.id, task.no_progress_rekicks, stuck_threshold,
                    )
                else:
                    log.info(
                        "skipped stuck-escalation of task %s: no longer in_progress "
                        "(self-completed before sweep)", task.id,
                    )
            elif task.retries >= max_retries:
                # Rung 4 — abandon backstop: progressing but never finishing.
                if fail_exhausted(conn, task, max_retries) is not None:
                    result.failed.append(task.id)
                    log.warning(
                        "force-abandoned task %s after %d retries (>= max %d)",
                        task.id, task.retries, max_retries,
                    )
                else:
                    log.info(
                        "skipped exhausted-fail of task %s: no longer in_progress "
                        "(self-completed before sweep)", task.id,
                    )
            else:
                # Rung 5 — re-kick (progress-aware).
                if rekick(conn, task) is not None:
                    result.rekicked.append(task.id)
                    log.info(
                        "re-kicked stale task %s (retry %d/%d)",
                        task.id, task.retries + 1, max_retries,
                    )
                else:
                    log.info(
                        "skipped re-kick of task %s: no longer stale/held "
                        "(changed before sweep)", task.id,
                    )
        except Exception:  # one bad task must not sink the whole sweep
            log.exception("supervisor failed to handle task %s", task.id)
    return result


# --- Reconnect grace window (anti thundering-herd, ADR-0017) -----------------


class GraceTracker:
    """Tracks DB connectivity to enforce a post-reconnect grace window.

    Prevents the **thundering-herd re-kick**: during a DB outage no worker can
    write a heartbeat, so on recovery *every* in-progress task's heartbeat is
    stale at once. Re-kicking them all immediately would stampede live workers
    that are about to heartbeat again. After a reconnect that follows a *known
    outage*, we defer sweeping for ``grace_s`` so those workers re-heartbeat
    first; only tasks still stale after the window are genuinely dropped.

    Pure, side-effect-free logic over an injectable ``monotonic`` clock, so the
    grace decision is unit-testable without sleeping or a database. A clean first
    connect (no prior failure) arms **no** window — startup re-kicks stay prompt.
    """

    def __init__(
        self,
        grace_s: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.grace_s = grace_s
        self._monotonic = monotonic
        self._db_down = False  # a connect/conn failure seen, not yet recovered
        self._grace_until: Optional[float] = None  # deadline while in a window

    def note_failure(self) -> None:
        """Record that the DB became unreachable (connect failed / conn dropped)."""
        self._db_down = True

    def note_connected(self) -> None:
        """Record a successful (re)connect.

        Arms the grace window **iff** recovering from a known outage; a clean
        first/steady connect arms nothing.
        """
        if self._db_down and self.grace_s > 0:
            self._grace_until = self._monotonic() + self.grace_s
        self._db_down = False

    def in_grace(self) -> bool:
        """True while inside the post-reconnect grace window (defer re-kicks).

        Self-clears once the window elapses, so a caller can poll it each loop.
        """
        if self._grace_until is None:
            return False
        if self._monotonic() >= self._grace_until:
            self._grace_until = None
            return False
        return True

    def grace_remaining(self) -> float:
        """Seconds left in the current grace window (0 if none / elapsed)."""
        if self._grace_until is None:
            return 0.0
        return max(0.0, self._grace_until - self._monotonic())


def supervised_sweep(
    conn: psycopg.Connection,
    grace: GraceTracker,
    threshold_s: float,
    max_retries: int,
    **sweep_kwargs,
) -> Optional[SweepResult]:
    """One supervised pass: honor the reconnect grace window, else sweep.

    Returns ``None`` when the sweep was **deferred** because we are inside the
    post-reconnect grace window (no re-kick happened — this is the anti
    thundering-herd guard), otherwise the :class:`SweepResult` from :func:`sweep`.
    Keeping this a distinct, tiny unit lets tests assert "no mass re-kick
    immediately on reconnect" without driving the forever-loop.
    """
    if grace.in_grace():
        log.info(
            "reconnect grace: deferring re-kick sweep for %.0fs so live workers "
            "re-heartbeat first (anti thundering-herd)",
            grace.grace_remaining(),
        )
        return None
    return sweep(conn, threshold_s, max_retries, **sweep_kwargs)


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
    grace_s: Optional[float] = None,
    nudge_grace_s: Optional[float] = None,
    stuck_threshold: Optional[int] = None,
) -> None:
    """Loop forever, sweeping every ``interval_s``. Thin — delegates to :func:`supervised_sweep`.

    Config falls back to env (then module defaults). DB-outage resilience (ADR-0017):

    - the connection is opened via :func:`runtime.db.connect_with_retry`, so an
      unreachable store surfaces as one :class:`DBUnavailable` (log degraded +
      retry next interval) — never a crash or a hang;
    - a :class:`GraceTracker` defers re-kicks for ``grace_s`` after recovering from
      an outage, so live workers re-heartbeat before the supervisor could re-kick
      them en masse (anti thundering-herd).

    The loop never lets one bad sweep kill the process: the supervisor is the
    guarantee of last resort, so it must keep running. launchd ``KeepAlive``
    restarts it if the process itself dies.
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
    grace_s = (
        _env_float("SUPERVISOR_RECONNECT_GRACE_S", DEFAULT_RECONNECT_GRACE_S)
        if grace_s is None
        else grace_s
    )
    nudge_grace_s = (
        _env_float("SUPERVISOR_NUDGE_GRACE_S", DEFAULT_NUDGE_GRACE_S)
        if nudge_grace_s is None
        else nudge_grace_s
    )
    stuck_threshold = (
        _env_int("SUPERVISOR_STUCK_THRESHOLD", DEFAULT_STUCK_THRESHOLD)
        if stuck_threshold is None
        else stuck_threshold
    )

    log.info(
        "supervisor starting: interval=%.0fs stale=%.0fs max_retries=%d grace=%.0fs "
        "nudge_grace=%.0fs stuck_threshold=%d",
        interval_s,
        threshold_s,
        max_retries,
        grace_s,
        nudge_grace_s,
        stuck_threshold,
    )
    grace = GraceTracker(grace_s)
    conn: Optional[psycopg.Connection] = None
    while True:
        try:
            if conn is None or conn.closed:
                # Bounded degraded connect: unreachable DB → DBUnavailable, handled
                # below (degrade + retry), never a crash/hang. A reconnect after a
                # prior failure arms the grace window (anti thundering-herd).
                conn = connect_with_retry(
                    on_retry=lambda n, d, e: log.warning(
                        "database unreachable (attempt %d, retrying in %.0fs): %s",
                        n, d, e,
                    )
                )
                grace.note_connected()
            result = supervised_sweep(
                conn, grace, threshold_s, max_retries,
                nudge_grace_s=nudge_grace_s, stuck_threshold=stuck_threshold,
            )
            if result is not None and result.acted:
                log.info(
                    "sweep: scanned=%d nudged=%d deferred=%d rekicked=%d stuck=%d failed=%d",
                    result.scanned,
                    len(result.nudged),
                    len(result.deferred),
                    len(result.rekicked),
                    len(result.stuck),
                    len(result.failed),
                )
        except DBUnavailable as exc:
            # Degraded mode: DB unreachable after bounded retries. Log + retry next
            # interval; mark the outage so the next successful connect arms grace.
            log.warning("database unavailable (degraded mode, will retry): %s", exc)
            grace.note_failure()
            conn = _safe_close(conn)
        except psycopg.OperationalError:
            # Connection dropped mid-sweep — treat as an outage so recovery is graced.
            log.exception("supervisor lost its database connection; will reconnect")
            grace.note_failure()
            conn = _safe_close(conn)
        except Exception:
            # A non-connectivity bug in a sweep must not kill the process, but it is
            # NOT an outage, so it does not arm the grace window.
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
