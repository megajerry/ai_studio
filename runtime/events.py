"""Append-only event log access (ADR-0004, ADR-0012).

Only two operations exist by design: append (insert) and read. There is no
update or delete path — the log is the immutable, replayable source of truth.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .models import Event, EventIn, make_event

_EVENT_COLUMNS = "id, ts, seq, task_id, workstream, type, payload, trace_id, span_id"


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
    since: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[Event]:
    """Read events in true append order, optionally filtered.

    Ordering is by ``seq`` (a monotonic identity assigned at insert), not ``ts``:
    ``ts`` defaults to the transaction start time, so events appended within one
    transaction share a timestamp and cannot be ordered by it. Filters compose
    (AND). ``since`` is exclusive on ``ts`` (a time-window filter); ``seq`` still
    dictates order.
    """
    clauses: list[str] = []
    params: list[object] = []
    if task_id is not None:
        clauses.append("task_id = %s")
        params.append(task_id)
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    if since is not None:
        clauses.append("ts > %s")
        params.append(since)

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
