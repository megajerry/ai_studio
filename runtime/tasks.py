"""Task-queue access (ADR-0004, ADR-0009, ADR-0010, ADR-0012).

Agents coordinate ONLY through this queue + the event log — never by direct
calls. Every state transition here appends a corresponding event in the same
transaction, so the log is a complete, replayable record of the queue.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .events import append_event
from .models import Assignee, EventType, Task, TaskStatus, make_event

_TASK_COLUMNS = (
    "id, workstream, type, status, priority, assignee, payload, result, "
    "heartbeat_at, claimed_by, budget_tokens, spent_tokens, created_at, updated_at"
)


def _emit(conn: psycopg.Connection, task: Task, event_type: EventType, **payload) -> None:
    append_event(
        conn,
        make_event(
            workstream=task.workstream,
            type=event_type.value,
            task_id=task.id,
            payload={"status": task.status.value, **payload},
        ),
    )


def enqueue_task(
    conn: psycopg.Connection,
    *,
    workstream: str,
    type: str,
    payload: Optional[dict] = None,
    priority: int = 0,
    assignee: Optional[Assignee] = None,
    budget_tokens: Optional[int] = None,
) -> Task:
    """Create a queued task and emit ``task.created``.

    Enqueueing is how a role/agent is "spawned" (ADR-0009): the runtime later
    materializes a worker to claim and service it.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO tasks (workstream, type, payload, priority, assignee, budget_tokens)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    workstream,
                    type,
                    Jsonb(payload or {}),
                    priority,
                    assignee.value if assignee else None,
                    budget_tokens,
                ),
            )
            task = Task.model_validate(cur.fetchone())
        _emit(conn, task, EventType.TASK_CREATED, type=task.type, priority=task.priority)
    return task


def claim_task(
    conn: psycopg.Connection,
    *,
    worker_id: str,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
) -> Optional[Task]:
    """Atomically claim the highest-priority queued task, or return None.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never claim the same
    task and never block on each other. A worker declaring ``assignee`` picks up
    tasks targeted at that assignee OR left unassigned (null); tasks targeted at
    the *other* assignee are skipped (ADR-0010). Sets the row ``in_progress``
    with an initial heartbeat and emits ``task.claimed``.
    """
    clauses = ["status = 'queued'"]
    params: list[object] = []
    if assignee is not None:
        clauses.append("(assignee IS NULL OR assignee = %s)")
        params.append(assignee.value)
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    where = " AND ".join(clauses)

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id FROM tasks
                WHERE {where}
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                params,
            )
            picked = cur.fetchone()
            if picked is None:
                return None

            cur.execute(
                f"""
                UPDATE tasks
                SET status = 'in_progress',
                    claimed_by = %s,
                    heartbeat_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (worker_id, picked["id"]),
            )
            task = Task.model_validate(cur.fetchone())
        _emit(conn, task, EventType.TASK_CLAIMED, claimed_by=worker_id)
    return task


def heartbeat(
    conn: psycopg.Connection, task_id: UUID, worker_id: str
) -> Optional[Task]:
    """Refresh a task's heartbeat; returns the task, or None if not held.

    Only the worker holding the claim may heartbeat (guards against a stale
    worker resurrecting a re-kicked task). Emits ``task.heartbeat``.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET heartbeat_at = now(), updated_at = now()
                WHERE id = %s AND claimed_by = %s AND status = 'in_progress'
                RETURNING {_TASK_COLUMNS}
                """,
                (task_id, worker_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task = Task.model_validate(row)
        _emit(conn, task, EventType.TASK_HEARTBEAT, claimed_by=worker_id)
    return task


def complete_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    result: Optional[dict] = None,
    status: TaskStatus = TaskStatus.DONE,
    spent_tokens: Optional[int] = None,
) -> Task:
    """Mark a task done/failed with a result and emit ``task.finished``.

    ``status`` must be a terminal state. ``spent_tokens`` (if given) records
    final cost for telemetry (ADR-0012).
    """
    if status not in (TaskStatus.DONE, TaskStatus.FAILED):
        raise ValueError("complete_task status must be 'done' or 'failed'")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET status = %s,
                    result = %s,
                    spent_tokens = COALESCE(%s, spent_tokens),
                    updated_at = now()
                WHERE id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    status.value,
                    Jsonb(result) if result is not None else None,
                    spent_tokens,
                    task_id,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise LookupError(f"task {task_id} not found")
            task = Task.model_validate(row)
        _emit(conn, task, EventType.TASK_FINISHED, spent_tokens=task.spent_tokens)
    return task


def find_stale_tasks(
    conn: psycopg.Connection, threshold_seconds: float
) -> list[Task]:
    """Return in-progress tasks whose heartbeat is older than the threshold.

    This is exactly what the non-agent supervisor (ADR-0004) polls to find
    silently-dropped tasks to re-kick. A null heartbeat also counts as stale.
    Ordered oldest-heartbeat-first (nulls first) so the most-neglected tasks
    surface earliest.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_TASK_COLUMNS} FROM tasks
            WHERE status = 'in_progress'
              AND (heartbeat_at IS NULL
                   OR heartbeat_at < now() - make_interval(secs => %s))
            ORDER BY heartbeat_at ASC NULLS FIRST
            """,
            (float(threshold_seconds),),
        )
        rows = cur.fetchall()
    return [Task.model_validate(r) for r in rows]
