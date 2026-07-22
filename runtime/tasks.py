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
    "heartbeat_at, claimed_by, budget_tokens, spent_tokens, retries, "
    "created_at, updated_at"
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
    worker resurrecting a re-kicked task). Deliberately emits **no** event:
    heartbeats are high-frequency liveness with zero replay value, and logging
    each one would bloat the append-only log (ADR-0013). ``EventType.TASK_HEARTBEAT``
    remains defined for occasional manual/explicit use.
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
    return Task.model_validate(row)


def complete_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    result: Optional[dict] = None,
    status: TaskStatus = TaskStatus.DONE,
    spent_tokens: Optional[int] = None,
    force: bool = False,
) -> Optional[Task]:
    """Mark a task done/failed with a result and emit ``task.finished``.

    ``status`` must be a terminal state. By default only an ``in_progress`` task
    is finalized — a worker cannot finalize a task it no longer owns (e.g. one
    the supervisor already re-kicked, or an already-terminal task). On such a
    conflict (or a missing task) the row is left untouched, **no event is
    emitted**, and ``None`` is returned.

    ``force=True`` bypasses the in-progress guard so the supervisor can
    force-fail a stale/re-kicked task regardless of its current state.
    ``spent_tokens`` (if given) records final cost for telemetry (ADR-0012).
    """
    if status not in (TaskStatus.DONE, TaskStatus.FAILED):
        raise ValueError("complete_task status must be 'done' or 'failed'")

    guard = "" if force else "AND status = 'in_progress'"
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET status = %s,
                    result = %s,
                    spent_tokens = COALESCE(%s, spent_tokens),
                    updated_at = now()
                WHERE id = %s {guard}
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
                return None
            task = Task.model_validate(row)
        _emit(conn, task, EventType.TASK_FINISHED, spent_tokens=task.spent_tokens)
    return task


def block_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    approval_id: UUID,
    reason: str = "",
) -> Optional[Task]:
    """Park an ``in_progress`` task as ``blocked`` on a pending 🔴 approval.

    Stores ``approval_id`` in ``result`` so :func:`runtime.worker.resume_approved`
    can match the task to its approval once a human resolves it. Guarded to
    ``in_progress`` (like :func:`heartbeat`) so a task that changed state is left
    untouched (returns ``None``).

    Emits **no** event by design: the ``approval.requested`` event (from
    :func:`runtime.enforce.invoke`) already records this block with the same
    ``task_id``, and ``blocked`` is a transient wait state — not a terminal
    verify→commit transition — so re-logging it would only bloat the log (ADR-0013).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET status = 'blocked',
                    result = %s,
                    updated_at = now()
                WHERE id = %s AND status = 'in_progress'
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    Jsonb({"blocked_on_approval": str(approval_id), "reason": reason}),
                    task_id,
                ),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def requeue_blocked_task(conn: psycopg.Connection, task_id: UUID) -> Optional[Task]:
    """Re-queue a ``blocked`` task once its approval is granted; guarded to ``blocked``.

    Clears the claim and heartbeat so a fresh worker re-claims it and re-runs the
    action — on that retry :func:`runtime.enforce.invoke` finds the live grant and
    executes. Emits no event here; the caller (:func:`runtime.worker.resume_approved`)
    emits ``approval.resumed`` with the grant context.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET status = 'queued',
                    claimed_by = NULL,
                    heartbeat_at = NULL,
                    updated_at = now()
                WHERE id = %s AND status = 'blocked'
                RETURNING {_TASK_COLUMNS}
                """,
                (task_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def find_blocked_tasks(conn: psycopg.Connection) -> list[Task]:
    """Return all tasks currently parked ``blocked`` on an approval (oldest first).

    :func:`runtime.worker.resume_approved` scans these to advance any whose
    approval a human has now resolved (grant → re-queue, deny → fail).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE status = 'blocked' ORDER BY created_at ASC"
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


def add_spent_tokens(
    conn: psycopg.Connection, task_id: UUID, tokens: int
) -> Optional[Task]:
    """Increment a task's ``spent_tokens`` by ``tokens`` (telemetry; ADR-0012).

    Called by the instrumented model-call wrapper (:func:`runtime.model.call.call_model`)
    after each LLM call so a task's cumulative token spend is tracked live for
    budget enforcement. Unlike :func:`complete_task` this does **not** change
    status and is not guarded to ``in_progress`` — spend is recorded wherever the
    task is. Returns the updated task, or ``None`` if the task does not exist.
    Emits no event: the ``model.call`` event (from the wrapper) already carries
    per-call tokens + cost, so incrementing the counter here would double-log.
    """
    if tokens < 0:
        raise ValueError("tokens must be non-negative")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET spent_tokens = spent_tokens + %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (tokens, task_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


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
    # The supervisor polls this on a long-lived connection; close the read's
    # implicit transaction so it is not left idle-in-transaction.
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


def rekick_task(conn: psycopg.Connection, task_id: UUID) -> Optional[Task]:
    """Re-queue a stale in-progress task for a fresh worker; emit ``task.rekicked``.

    This is the non-agent supervisor's core action (ADR-0004): a task whose
    worker went silent is reset to ``queued`` with its claim and heartbeat
    cleared and ``retries`` incremented, so the runtime can materialize a fresh
    worker to service it. Guarded to ``in_progress`` (like :func:`heartbeat`)
    so a task that changed state between the supervisor's scan and this write is
    left untouched (returns ``None``, no event). Runs in one transaction so the
    state change and its event commit atomically.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET status = 'queued',
                    claimed_by = NULL,
                    heartbeat_at = NULL,
                    retries = retries + 1,
                    updated_at = now()
                WHERE id = %s AND status = 'in_progress'
                RETURNING {_TASK_COLUMNS}
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task = Task.model_validate(row)
        _emit(conn, task, EventType.TASK_REKICKED, retries=task.retries)
    return task
