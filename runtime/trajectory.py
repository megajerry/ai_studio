"""The single guarded writer for reasoning TRAJECTORIES (ADR-0020).

The runtime already records ACTIONS + STATE (``task_transitions``, the event log,
``model.call`` telemetry). This module records the missing layer: the ordered
causal **trajectory** of how a role — especially the PM — reached a decision (what
it observed, options weighed, what it decided + why, where the Critic pushed back,
what it revised, when it escalated). A trajectory is a bounded reasoning episode;
its steps are an append-only, gapless, per-trajectory ``seq``-ordered chain.

Discipline (mirrors :func:`runtime.tasks.transition` + :mod:`runtime.events`):

- **Single guarded writer.** ALL writes to ``trajectories`` / ``trajectory_steps``
  go through this module — there are no ad-hoc INSERT/UPDATEs of these tables
  elsewhere. Each write runs in one transaction so the row write + its body-free
  event are atomic (psycopg nests the event append as a savepoint).
- **Gapless per-trajectory seq.** :func:`add_step` locks the parent trajectory row
  ``FOR UPDATE`` and assigns ``max(seq)+1``, so concurrent appends serialize with
  no gaps/races (the same discipline as the guarded task transition; the DB
  ``UNIQUE(trajectory_id, seq)`` index backstops it).
- **Verbatim on the write path.** Full ``rationale`` bodies are stored inline (no
  scrubbing). Footprint is bounded later by a TTL (:func:`expire_trajectories`)
  and a verbatim→lean rotation (:func:`compact_to_lean`) run by a learning agent.
- **Body-free events (invariants 5 & 6).** Every write emits a ``trajectory.*``
  event carrying ONLY ids / types / seq / step_type / counts — NEVER the
  goal/summary/rationale/outcome text. Those bodies live in the LOCAL DB ONLY.
- **Injectable ``now``.** Every write accepts ``now`` (a ``datetime``); when given
  it fixes the timestamp (``COALESCE(%s, now())``) so tests are deterministic,
  otherwise the DB clock is the source of truth (as elsewhere in the repo).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from .event_types import (
    EVENT_TRAJECTORY_CLOSED,
    EVENT_TRAJECTORY_COMPACTED,
    EVENT_TRAJECTORY_EXPIRED,
    EVENT_TRAJECTORY_STARTED,
    EVENT_TRAJECTORY_STEP_ADDED,
)
from .events import append_event
from .models import make_event

# --- Vocabulary (closed sets, mirrored by the migration CHECK constraints) ---

RETENTION_VERBATIM = "verbatim"
RETENTION_LEAN = "lean"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

#: The kinds of reasoning step a trajectory records (ADR-0020). Kept as a closed
#: vocabulary so the causal chain has a stable, analyzable shape.
STEP_TYPES: frozenset[str] = frozenset({
    "observe", "plan", "decide", "consult",
    "revise", "decompose", "escalate", "commit",
})

_TRAJECTORY_COLUMNS = (
    "id, role, workstream, goal, status, retention_tier, started_at, ended_at, "
    "expires_at, context_size_start, context_size_peak, tokens, cost_usd, "
    "latency_ms, outcome_summary, created_at"
)
_STEP_COLUMNS = (
    "id, trajectory_id, seq, step_type, summary, rationale, options_considered, "
    "choice, confidence, refs, context_size, tokens, cost_usd, latency_ms, created_at"
)


# --- Row models -------------------------------------------------------------


class Trajectory(BaseModel):
    """A persisted reasoning-episode row."""

    id: UUID
    role: str
    workstream: str
    goal: str
    status: str
    retention_tier: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    context_size_start: Optional[int] = None
    context_size_peak: Optional[int] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    outcome_summary: Optional[str] = None
    created_at: datetime


class TrajectoryStep(BaseModel):
    """A persisted, append-only step within a trajectory (ordered by ``seq``)."""

    id: UUID
    trajectory_id: UUID
    seq: int
    step_type: str
    summary: str
    rationale: Optional[str] = None
    options_considered: Any = Field(default_factory=list)
    choice: Optional[str] = None
    confidence: Optional[float] = None
    refs: Any = Field(default_factory=dict)
    context_size: Optional[int] = None
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    created_at: datetime


def _to_trajectory(row: dict) -> Trajectory:
    data = dict(row)
    if data.get("cost_usd") is not None:
        data["cost_usd"] = float(data["cost_usd"])
    return Trajectory.model_validate(data)


def _to_step(row: dict) -> TrajectoryStep:
    data = dict(row)
    if data.get("cost_usd") is not None:
        data["cost_usd"] = float(data["cost_usd"])
    return TrajectoryStep.model_validate(data)


def _emit(conn: psycopg.Connection, *, workstream: str, type: str, **payload: Any) -> None:
    """Append a BODY-FREE ``trajectory.*`` event (ids/types/seq/counts only).

    Deliberately never carries goal / summary / rationale / outcome text — those
    bodies live in the local DB only (invariants 5 & 6). Runs inside the caller's
    open transaction (psycopg nests it as a savepoint) so the row write + event are
    atomic and replayable.
    """
    append_event(conn, make_event(workstream=workstream, type=type, payload=payload))


# --- start ------------------------------------------------------------------


def start_trajectory(
    conn: psycopg.Connection,
    role: str,
    workstream: str,
    goal: str,
    *,
    ttl: Optional[float] = None,
    context_size_start: Optional[int] = None,
    now: Optional[datetime] = None,
) -> UUID:
    """Open a ``verbatim`` trajectory for ``role`` in ``workstream`` toward ``goal``.

    ``ttl`` (seconds) sets ``expires_at = started_at + ttl`` (the TTL horizon a
    later :func:`expire_trajectories` sweep enforces); ``None`` = never expires.
    Emits a body-free ``trajectory.started`` (id + role only) and returns the new
    trajectory id. ``now`` fixes ``started_at``/``expires_at`` for deterministic
    tests; otherwise the DB clock is used.
    """
    if not role:
        raise ValueError("trajectory role must be non-empty")
    if not workstream:
        raise ValueError("trajectory workstream must be non-empty")
    if not goal or not goal.strip():
        raise ValueError("trajectory goal must be non-empty")
    ttl_s = None if ttl is None else float(ttl)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO trajectories
                    (role, workstream, goal, status, retention_tier,
                     started_at, expires_at, context_size_start)
                VALUES (
                    %s, %s, %s, 'open', 'verbatim',
                    COALESCE(%s::timestamptz, now()),
                    CASE WHEN %s IS NULL THEN NULL
                         ELSE COALESCE(%s::timestamptz, now()) + make_interval(secs => %s)
                    END,
                    %s
                )
                RETURNING {_TRAJECTORY_COLUMNS}
                """,
                (role, workstream, goal, now, ttl_s, now, ttl_s, context_size_start),
            )
            traj = _to_trajectory(cur.fetchone())
        _emit(conn, workstream=workstream, type=EVENT_TRAJECTORY_STARTED,
              trajectory_id=str(traj.id), role=role)
    return traj.id


# --- append a step ----------------------------------------------------------


def add_step(
    conn: psycopg.Connection,
    trajectory_id: UUID,
    step_type: str,
    summary: str,
    *,
    rationale: Optional[str] = None,
    options_considered: Optional[Any] = None,
    choice: Optional[str] = None,
    confidence: Optional[float] = None,
    refs: Optional[Any] = None,
    context_size: Optional[int] = None,
    tokens: Optional[int] = None,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    now: Optional[datetime] = None,
) -> TrajectoryStep:
    """Append one reasoning step, assigning the next gapless per-trajectory ``seq``.

    Locks the parent trajectory row ``FOR UPDATE`` and takes ``max(seq)+1`` so
    concurrent appends serialize with no gaps or races (the ``UNIQUE(trajectory_id,
    seq)`` index backstops it). ``rationale`` is the FULL VERBATIM body (stored
    inline). If ``now`` is given it also refreshes the trajectory's
    ``context_size_peak`` when ``context_size`` exceeds the current peak. Emits a
    body-free ``trajectory.step_added`` (ids + seq + step_type only) and returns
    the persisted step.
    """
    if step_type not in STEP_TYPES:
        raise ValueError(f"unknown step_type {step_type!r} (allowed: {sorted(STEP_TYPES)})")
    if not summary or not summary.strip():
        raise ValueError("trajectory step summary must be non-empty")

    with conn.transaction():
        with conn.cursor() as cur:
            # Lock the parent so seq assignment for THIS trajectory serializes.
            cur.execute(
                "SELECT id, workstream FROM trajectories WHERE id = %s FOR UPDATE",
                (trajectory_id,),
            )
            parent = cur.fetchone()
            if parent is None:
                raise ValueError(f"trajectory {trajectory_id} not found")
            workstream = parent["workstream"]

            cur.execute(
                "SELECT COALESCE(max(seq), 0) + 1 AS next FROM trajectory_steps "
                "WHERE trajectory_id = %s",
                (trajectory_id,),
            )
            seq = int(cur.fetchone()["next"])

            cur.execute(
                f"""
                INSERT INTO trajectory_steps
                    (trajectory_id, seq, step_type, summary, rationale,
                     options_considered, choice, confidence, refs,
                     context_size, tokens, cost_usd, latency_ms, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::timestamptz, now()))
                RETURNING {_STEP_COLUMNS}
                """,
                (
                    trajectory_id, seq, step_type, summary, rationale,
                    Jsonb(options_considered if options_considered is not None else []),
                    choice, confidence,
                    Jsonb(refs if refs is not None else {}),
                    context_size, tokens, cost_usd, latency_ms, now,
                ),
            )
            step = _to_step(cur.fetchone())

            # Track the peak context size seen over the episode (ADR-0013).
            if context_size is not None:
                cur.execute(
                    "UPDATE trajectories "
                    "SET context_size_peak = GREATEST(COALESCE(context_size_peak, 0), %s) "
                    "WHERE id = %s",
                    (context_size, trajectory_id),
                )

        _emit(conn, workstream=workstream, type=EVENT_TRAJECTORY_STEP_ADDED,
              trajectory_id=str(trajectory_id), step_id=str(step.id),
              seq=step.seq, step_type=step.step_type)
    return step


# --- close ------------------------------------------------------------------


def close_trajectory(
    conn: psycopg.Connection,
    trajectory_id: UUID,
    *,
    outcome_summary: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Trajectory]:
    """Close an open trajectory, stamping ``ended_at`` + total wall-clock latency.

    Sets ``status='closed'``, ``ended_at`` (from ``now`` or the DB clock), the
    optional ``outcome_summary`` body (local DB only), and ``latency_ms`` =
    ``ended_at - started_at``. Guarded to an ``open`` trajectory (returns ``None``
    if missing or already closed). Emits a body-free ``trajectory.closed``.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE trajectories
                SET status = 'closed',
                    ended_at = COALESCE(%s::timestamptz, now()),
                    outcome_summary = COALESCE(%s, outcome_summary),
                    latency_ms = (EXTRACT(EPOCH FROM (
                        COALESCE(%s::timestamptz, now()) - started_at)) * 1000)::bigint
                WHERE id = %s AND status = 'open'
                RETURNING {_TRAJECTORY_COLUMNS}
                """,
                (now, outcome_summary, now, trajectory_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            traj = _to_trajectory(row)
        _emit(conn, workstream=traj.workstream, type=EVENT_TRAJECTORY_CLOSED,
              trajectory_id=str(traj.id))
    return traj


# --- read helpers -----------------------------------------------------------


def get_trajectory(conn: psycopg.Connection, trajectory_id: UUID) -> Optional[Trajectory]:
    """Fetch one trajectory by id, or ``None`` if absent."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TRAJECTORY_COLUMNS} FROM trajectories WHERE id = %s",
            (trajectory_id,),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return _to_trajectory(row) if row else None


def list_steps(conn: psycopg.Connection, trajectory_id: UUID) -> list[TrajectoryStep]:
    """Return a trajectory's steps in true causal order (by ``seq``)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_STEP_COLUMNS} FROM trajectory_steps "
            "WHERE trajectory_id = %s ORDER BY seq ASC",
            (trajectory_id,),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [_to_step(r) for r in rows]


# --- TTL expiry -------------------------------------------------------------


def expire_trajectories(conn: psycopg.Connection, now: Optional[datetime] = None) -> int:
    """Hard-delete every trajectory past its ``expires_at`` at ``now``; return count.

    Deletion (not marking) is deliberate: the whole point of the TTL is to bound
    the LOCAL footprint of verbatim bodies (ADR-0020), so an expired episode is
    removed outright. ``ON DELETE CASCADE`` drops its steps; the ``tasks``
    back-reference is ``ON DELETE SET NULL`` so a linked task is never orphaned.
    Trajectories with ``expires_at IS NULL`` never expire. Emits one body-free
    ``trajectory.expired`` per deleted trajectory (id only).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM trajectories
                WHERE expires_at IS NOT NULL
                  AND expires_at <= COALESCE(%s::timestamptz, now())
                RETURNING id, workstream
                """,
                (now,),
            )
            deleted = cur.fetchall()
        for row in deleted:
            _emit(conn, workstream=row["workstream"], type=EVENT_TRAJECTORY_EXPIRED,
                  trajectory_id=str(row["id"]))
    return len(deleted)


# --- verbatim → lean rotation -----------------------------------------------


def _default_distill(rationale: Optional[str]) -> Optional[str]:
    """Default lean distillation: keep the first line, truncated (loses no
    outcome-relevant field — those live in choice/confidence/refs, not here)."""
    if rationale is None:
        return None
    first_line = rationale.splitlines()[0] if rationale.splitlines() else rationale
    return first_line[:200]


def compact_to_lean(
    conn: psycopg.Connection,
    trajectory_id: UUID,
    distill_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    *,
    now: Optional[datetime] = None,
) -> Optional[Trajectory]:
    """Rotate a trajectory ``verbatim`` → ``lean`` (the learning agent's hook).

    Replaces each step's full verbatim ``rationale`` with a distilled form
    (``distill_fn(rationale)``, defaulting to the first-line truncation) and sets
    ``retention_tier='lean'``. Outcome-relevant fields are PRESERVED untouched:
    ``choice`` / ``confidence`` / ``refs`` on each step and the trajectory's
    ``outcome_summary``. Idempotent on an already-lean trajectory (re-distilling
    the distilled text is a no-op-ish shrink). Returns the updated trajectory, or
    ``None`` if it does not exist. Emits a body-free ``trajectory.compacted``
    (id + tier + step count).
    """
    distill = distill_fn or _default_distill
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, workstream FROM trajectories WHERE id = %s FOR UPDATE",
                (trajectory_id,),
            )
            parent = cur.fetchone()
            if parent is None:
                return None
            workstream = parent["workstream"]

            cur.execute(
                "SELECT id, rationale FROM trajectory_steps "
                "WHERE trajectory_id = %s ORDER BY seq ASC",
                (trajectory_id,),
            )
            steps = cur.fetchall()
            for s in steps:
                # Only the verbatim rationale body is distilled; choice/confidence/
                # refs are left exactly as stored (outcome-relevant, lossless).
                cur.execute(
                    "UPDATE trajectory_steps SET rationale = %s WHERE id = %s",
                    (distill(s["rationale"]), s["id"]),
                )

            cur.execute(
                f"""
                UPDATE trajectories SET retention_tier = 'lean'
                WHERE id = %s
                RETURNING {_TRAJECTORY_COLUMNS}
                """,
                (trajectory_id,),
            )
            traj = _to_trajectory(cur.fetchone())
        _emit(conn, workstream=workstream, type=EVENT_TRAJECTORY_COMPACTED,
              trajectory_id=str(trajectory_id), retention_tier=RETENTION_LEAN,
              steps_compacted=len(steps))
    return traj


__all__ = [
    "Trajectory",
    "TrajectoryStep",
    "RETENTION_VERBATIM",
    "RETENTION_LEAN",
    "STATUS_OPEN",
    "STATUS_CLOSED",
    "STEP_TYPES",
    "start_trajectory",
    "add_step",
    "close_trajectory",
    "get_trajectory",
    "list_steps",
    "expire_trajectories",
    "compact_to_lean",
]
