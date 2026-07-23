"""Periodic trajectory maintenance — TTL expiry + verbatim→lean rotation (ADR-0020).

The trajectory store keeps FULL VERBATIM reasoning bodies on the write path (fast,
no inline scrubbing). Footprint is bounded *later*, by this non-LLM periodic job —
the maintenance twin of :mod:`runtime.scheduler` / :mod:`runtime.supervisor`. It
wakes on a cadence, does bounded work, and exits each iteration:

1. **TTL expiry (always on).** Calls :func:`runtime.trajectory.expire_trajectories`
   to hard-delete every trajectory past its ``expires_at`` (ADR-0020). Safe and
   footprint-bounding, so it runs every sweep.
2. **verbatim→lean rotation (opt-in).** Distills OLD, already-MINED ``verbatim``
   episodes to the ``lean`` tier via :func:`runtime.trajectory.compact_to_lean`
   (choice/confidence/refs/outcome preserved; only the verbatim ``rationale`` body
   is shrunk). "Mined" = the learning/Retro agent has already extracted lessons
   from the episode, signalled by a ``retro.completed`` event that references the
   trajectory (:data:`runtime.event_types.EVENT_RETRO_COMPLETED`, payload
   ``trajectory_id``). Rotating only mined episodes guarantees no un-mined verbatim
   reasoning is distilled before the learning loop has read it. Rotation is
   **off by default** (``TRAJECTORY_ROTATE_ENABLED``) because it mutates bodies —
   an operator (or the launchd plist) opts in.

Like the scheduler/supervisor this is a **non-LLM singleton**, and it is
**DB-outage-safe (ADR-0017)**: the loop connects via
:func:`runtime.db.connect_with_retry` (an unreachable store surfaces as one
:class:`runtime.db.DBUnavailable` — log degraded + retry next interval, never a
crash/hang), and a single failing rotation never sinks the whole sweep. Run it::

    python -m runtime.trajectory_worker

Config (env, with sane defaults):

=================================  =========  ==============================================
Env var                            Default    Meaning
=================================  =========  ==============================================
``TRAJECTORY_WORKER_INTERVAL_S``   3600       Seconds between maintenance sweeps.
``TRAJECTORY_ROTATE_AFTER_S``      604800     Age (since ``ended_at``) past which a mined
                                              verbatim episode is eligible to rotate (7d).
``TRAJECTORY_ROTATE_ENABLED``      0 (off)    Enable the verbatim→lean rotation pass.
=================================  =========  ==============================================

The reusable selection + rotation functions (:func:`select_rotatable`,
:func:`rotate_mined_trajectories`) are also the hook the Retro role invokes when
the learning agent DECIDES to rotate (see :mod:`runtime.roles.retro`).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from uuid import UUID

import psycopg

from .db import DBUnavailable, connect_with_retry
from .event_types import EVENT_RETRO_COMPLETED
from .trajectory import compact_to_lean, expire_trajectories

log = logging.getLogger("runtime.trajectory_worker")

# --- Defaults (overridable via env) -----------------------------------------
DEFAULT_INTERVAL_S = 3600.0            # hourly maintenance sweep
DEFAULT_ROTATE_AFTER_S = 7 * 24 * 3600.0  # rotate mined verbatim older than 7 days
#: Rotation mutates bodies, so it is OFF by default — an operator opts in (this
#: mirrors the repo convention of a maintenance capability an operator enables).
DEFAULT_ROTATE_ENABLED = False

# Injectable seams so the sweep/rotation are unit-testable without a real DB.
Expirer = Callable[..., int]
Compactor = Callable[..., object]


# --- selection + rotation (also the Retro learning-agent hook) --------------


def select_rotatable(
    conn: psycopg.Connection,
    *,
    older_than_s: float,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[UUID]:
    """Ids of ``verbatim`` trajectories ripe for verbatim→lean rotation.

    A trajectory is *ripe* iff it is ``retention_tier='verbatim'`` AND
    ``status='closed'`` AND its ``ended_at`` is at least ``older_than_s`` seconds
    before ``now`` (the DB clock when ``now`` is ``None``) AND it has been **mined**
    by the learning/Retro agent — i.e. a ``retro.completed`` event references it
    (payload ``trajectory_id``). Ordered oldest-first. ``verbatim``-only selection
    makes rotation idempotent: an already-``lean`` trajectory is never re-selected.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id
            FROM trajectories t
            WHERE t.retention_tier = 'verbatim'
              AND t.status = 'closed'
              AND t.ended_at IS NOT NULL
              AND t.ended_at <= COALESCE(%s::timestamptz, now())
                                - make_interval(secs => %s::double precision)
              AND EXISTS (
                  SELECT 1 FROM events e
                  WHERE e.type = %s
                    AND e.payload->>'trajectory_id' = t.id::text
              )
            ORDER BY t.ended_at ASC
            LIMIT %s
            """,
            (now, float(older_than_s), EVENT_RETRO_COMPLETED, limit),
        )
        rows = cur.fetchall()
    # Don't leave a long-lived worker connection idle-in-transaction after a read.
    if not conn.autocommit:
        conn.commit()
    return [r["id"] for r in rows]


def rotate_mined_trajectories(
    conn: psycopg.Connection,
    *,
    older_than_s: float,
    now: Optional[datetime] = None,
    distill_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    limit: Optional[int] = None,
    compact: Compactor = compact_to_lean,
) -> list[UUID]:
    """Distill every ripe (old + mined) ``verbatim`` trajectory to ``lean``.

    Selects via :func:`select_rotatable` and calls the guarded
    :func:`runtime.trajectory.compact_to_lean` on each (choice/confidence/refs/
    outcome preserved; only the verbatim ``rationale`` body is shrunk). Idempotent
    (verbatim-only selection). Behavior-preserving on outcome-relevant fields.
    Degrades per-item (ADR-0017): a single failing rotation is logged and skipped
    so it never sinks the rest of the pass. Returns the ids actually rotated.
    """
    rotated: list[UUID] = []
    for tid in select_rotatable(conn, older_than_s=older_than_s, now=now, limit=limit):
        try:
            if compact(conn, tid, distill_fn, now=now) is not None:
                rotated.append(tid)
        except Exception:  # one bad rotation must not sink the whole pass
            log.exception("trajectory rotation failed for %s; skipping", tid)
    return rotated


# --- one maintenance sweep (the single, testable unit) ----------------------


@dataclass
class SweepResult:
    """What one maintenance sweep did — returned for logging/telemetry and tests."""

    expired: int = 0
    rotated: list[UUID] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return bool(self.expired or self.rotated)


def sweep_once(
    conn: psycopg.Connection,
    *,
    rotate_after_s: float,
    rotate_enabled: bool,
    now: Optional[datetime] = None,
    distill_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    expire: Expirer = expire_trajectories,
    rotate: Callable[..., list[UUID]] = rotate_mined_trajectories,
) -> SweepResult:
    """Run one maintenance pass: TTL expiry (always) + rotation (if enabled).

    The two phases are independent guarded operations, so a failure in one does not
    corrupt the other. ``expire``/``rotate`` are injectable seams for tests.
    """
    result = SweepResult()
    # (a) TTL — always safe, bounds the local verbatim footprint (ADR-0020).
    result.expired = expire(conn, now=now)
    # (b) verbatim→lean rotation — opt-in (mutates bodies), old + mined only.
    if rotate_enabled:
        result.rotated = rotate(
            conn, older_than_s=rotate_after_s, now=now, distill_fn=distill_fn
        )
    return result


# --- config + loop ----------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _safe_close(conn: Optional[psycopg.Connection]) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return None


def run(
    *,
    interval_s: Optional[float] = None,
    rotate_after_s: Optional[float] = None,
    rotate_enabled: Optional[bool] = None,
    connect: Callable[..., psycopg.Connection] = connect_with_retry,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: Optional[int] = None,
) -> None:
    """Loop, sweeping every ``interval_s``. Thin — delegates to :func:`sweep_once`.

    Config falls back to env then module defaults. DB-outage resilience (ADR-0017):
    the connection is opened via :func:`runtime.db.connect_with_retry`, so an
    unreachable store surfaces as one :class:`DBUnavailable` (log degraded + retry
    next interval) — never a crash or hang; a connection dropped mid-sweep is
    likewise degraded and reconnected. A non-connectivity bug in a sweep is logged
    but never kills the process. launchd ``KeepAlive`` restarts it if it dies.

    ``connect``/``sleep``/``max_iterations`` are injectable so the loop's degraded
    behavior is unit-testable without a real DB or real waiting (``max_iterations``
    bounds the otherwise-forever loop; ``None`` = run forever).
    """
    interval_s = (
        _env_float("TRAJECTORY_WORKER_INTERVAL_S", DEFAULT_INTERVAL_S)
        if interval_s is None
        else interval_s
    )
    rotate_after_s = (
        _env_float("TRAJECTORY_ROTATE_AFTER_S", DEFAULT_ROTATE_AFTER_S)
        if rotate_after_s is None
        else rotate_after_s
    )
    rotate_enabled = (
        _env_bool("TRAJECTORY_ROTATE_ENABLED", DEFAULT_ROTATE_ENABLED)
        if rotate_enabled is None
        else rotate_enabled
    )

    log.info(
        "trajectory_worker starting: interval=%.0fs rotate_after=%.0fs rotate_enabled=%s",
        interval_s, rotate_after_s, rotate_enabled,
    )
    conn: Optional[psycopg.Connection] = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            if conn is None or conn.closed:
                conn = connect(
                    on_retry=lambda n, d, e: log.warning(
                        "database unreachable (attempt %d, retrying in %.0fs): %s",
                        n, d, e,
                    )
                )
            result = sweep_once(
                conn, rotate_after_s=rotate_after_s, rotate_enabled=rotate_enabled
            )
            if result.acted:
                log.info(
                    "sweep: expired=%d rotated=%d", result.expired, len(result.rotated)
                )
        except DBUnavailable as exc:
            # Degraded mode: DB unreachable after bounded retries. Log + retry next
            # interval (ADR-0017) — never crash.
            log.warning("database unavailable (degraded mode, will retry): %s", exc)
            conn = _safe_close(conn)
        except psycopg.OperationalError:
            # Connection dropped mid-sweep — reconnect next interval.
            log.exception("trajectory_worker lost its database connection; will reconnect")
            conn = _safe_close(conn)
        except Exception:
            # A non-connectivity bug in a sweep must not kill the maintenance loop.
            log.exception("trajectory_worker sweep failed; will retry after interval")
            conn = _safe_close(conn)
        if max_iterations is None or iterations < max_iterations:
            sleep(interval_s)
    _safe_close(conn)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
