"""Append-only event log access (ADR-0004, ADR-0012).

Only two operations exist by design: append (insert) and read. There is no
update or delete path — the log is the immutable, replayable source of truth.

The real ``seq`` guarantee (read this before trusting a cursor)
-------------------------------------------------------------
``events.seq`` is a ``GENERATED ... AS IDENTITY`` column (migration 0004). That
gives it two properties that are easy to mis-state, so state them honestly:

- It is **insert-order**, NOT **commit-order**. The value is drawn from the
  identity sequence at ``INSERT`` time but only becomes *visible to other
  sessions at COMMIT*. So if writer A draws ``seq=N`` and holds its transaction
  open while writer B draws ``seq=N+1`` and commits, a reader sees ``N+1`` before
  ``N`` ever appears — commit order (``N+1`` then ``N``) differs from seq order.
- It is **strictly increasing but NOT gapless**. Identity values are consumed
  eagerly and are **burned on rollback** (a rolled-back INSERT permanently skips
  its drawn value), so the live log legitimately contains holes.

Consequences for a ``since_seq`` cursor consumer:

- A **plain** ``read_events(since_seq=cursor)`` (``lookback=0``) is **best-effort**.
  If it advances its cursor to the max seq it saw while a *lower* seq is still
  un-committed, that lower seq — once it commits — is ``< cursor`` and is
  **permanently skipped** by that consumer. (Verified: see
  ``runtime/tests/test_events_seq_visibility_db.py``.)
- A **full replay** (``since_seq=0`` / ``None``) is always **complete**: it reads
  every committed row, so no ordering hazard applies — only *incremental cursor*
  consumers are exposed.
- The robust incremental idiom is a **bounded lookback overlap**
  (``read_events(..., since_seq=cursor, lookback=K)``) combined with **idempotent
  processing**: the consumer re-scans ``K`` seqs behind its cursor each pass, so a
  seq that commits out-of-order is re-observed and re-processed (idempotency
  drops the duplicate). This recovers any late-committing seq **as long as fewer
  than ``K`` higher seqs were consumed before it committed** — a transaction held
  open across more than ``K`` intervening commits could still be missed, so it is
  a *bounded* guarantee, not an absolute one. Pick ``K`` well above the largest
  expected concurrent-write burst. See :data:`DEFAULT_LOOKBACK` and
  :func:`read_events`.

A stateless *commit-order* guarantee was evaluated and rejected: the canonical
"only consume rows whose inserting xid is below ``pg_snapshot_xmin``" watermark is
unsafe here because seq-draw order and xid-assignment order genuinely diverge in
this codebase (a task-state transaction acquires its xid by writing ``tasks`` /
``task_transitions`` *before* it appends its event, so a committed event can have
an xid below the horizon while a lower seq is still in-flight), and a
gap-contiguity watermark stalls on the log's legitimate rollback burns. The
bounded lookback above is the honest, non-stalling fix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .models import Event, EventIn, make_event

_EVENT_COLUMNS = "id, ts, seq, task_id, workstream, type, payload, trace_id, span_id"

#: Default bounded lookback overlap (in ``seq`` units) for an incremental
#: ``since_seq`` consumer that opts into commit-safety. Each pass re-scans this
#: many seqs behind the persisted cursor so an out-of-order/late-committing seq is
#: re-observed and (with idempotent processing) recovered rather than skipped. It
#: is generous relative to any realistic concurrent-write burst but still bounded
#: (a re-scan is cheap because type/task filters apply before the row limit).
DEFAULT_LOOKBACK = 1000


def append_event(conn: psycopg.Connection, event: EventIn) -> Event:
    """Insert one event and return the persisted row (id + ts assigned by DB).

    Runs in its own transaction so the write is atomic; when composed inside a
    larger transaction (e.g. a task state change) psycopg nests it as a
    savepoint, keeping the event and the state change all-or-nothing.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO events (task_id, workstream, type, payload, trace_id, span_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_EVENT_COLUMNS}
                """,
                (
                    event.task_id,
                    event.workstream,
                    event.type,
                    Jsonb(event.payload),
                    event.trace_id,
                    event.span_id,
                ),
            )
            row = cur.fetchone()
    return Event.model_validate(row)


def read_events(
    conn: psycopg.Connection,
    *,
    task_id: Optional[UUID] = None,
    workstream: Optional[str] = None,
    type: Optional[str] = None,
    since: Optional[datetime] = None,
    since_seq: Optional[int] = None,
    lookback: int = 0,
    limit: Optional[int] = None,
) -> list[Event]:
    """Read events in ``seq`` (insert) order, optionally filtered.

    Ordering is by ``seq`` (an identity assigned at insert), not ``ts``: ``ts``
    defaults to the transaction start time, so events appended within one
    transaction share a timestamp and cannot be ordered by it. **Note ``seq`` is
    insert-order, not commit-order, and is not gapless** — see the module
    docstring for the full guarantee. Filters compose (AND). ``type`` selects a
    single event-type wire string (e.g. a consumer scanning only ``task.stuck``).
    ``since`` is exclusive on ``ts`` (a time-window filter); ``seq`` still dictates
    order.

    ``since_seq`` is exclusive on ``seq`` — the cursor a consumer (e.g. the
    Spokesman notifier, or the PM replan dispatcher) persists to read only *new*
    events. On its own it is **best-effort**: a seq that commits *after* the
    consumer has advanced its cursor past it is skipped (see module docstring).

    ``lookback`` (default ``0`` = the classic best-effort read) makes the read
    **commit-safe by overlap**: the effective lower bound becomes ``since_seq -
    lookback`` (never below 0), so the consumer re-scans a bounded window behind
    its cursor and re-observes any seq that committed out-of-order. Combined with
    **idempotent processing** the re-observed event is recovered rather than lost.
    ``lookback`` only widens the window when ``since_seq`` is set (a full replay
    already reads everything); it is bounded (a transaction open across more than
    ``lookback`` intervening commits can still be missed). See
    :data:`DEFAULT_LOOKBACK`.
    """
    if lookback < 0:
        raise ValueError(f"lookback must be >= 0, got {lookback}")
    clauses: list[str] = []
    params: list[object] = []
    if task_id is not None:
        clauses.append("task_id = %s")
        params.append(task_id)
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    if type is not None:
        clauses.append("type = %s")
        params.append(type)
    if since is not None:
        clauses.append("ts > %s")
        params.append(since)
    if since_seq is not None:
        # Bounded lookback overlap: re-scan `lookback` seqs behind the cursor so a
        # seq that committed out-of-order (below an already-advanced cursor) is
        # re-observed. `lookback=0` reproduces the classic exclusive `seq > cursor`.
        clauses.append("seq > %s")
        params.append(max(0, since_seq - lookback))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} FROM events {where} ORDER BY seq ASC {limit_sql}",
            params,
        )
        rows = cur.fetchall()
    # Close the read's implicit transaction so a long-lived (e.g. supervisor)
    # connection is not left idle-in-transaction. No-op under autocommit.
    if not conn.autocommit:
        conn.commit()
    return [Event.model_validate(r) for r in rows]


# Re-exported for convenience so producers can build + append in one import.
__all__ = ["append_event", "read_events", "make_event"]
