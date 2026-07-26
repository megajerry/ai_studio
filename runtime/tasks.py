"""Task-queue access + the canonical lifecycle (ADR-0004/0009/0010/0012/0015).

Agents coordinate ONLY through this queue + the event log — never by direct
calls. Every state change goes through the single guarded :func:`transition`
(there are **no ad-hoc status UPDATEs** anywhere): it checks the move against the
canonical state machine (:mod:`runtime.task_state`), does the UPDATE guarded on
the current status, records a ``task_transitions`` telemetry row (with latency),
and emits a ``task.transition`` event — all in one transaction, so the log +
telemetry are a complete, replayable record of the queue.

Work is picked up with :func:`grab_task` (grab-by-sort, ``FOR UPDATE SKIP
LOCKED``, dependency-gated); :func:`claim_task` is the convenience grab→start the
runtime loop uses. Tasks with unmet prerequisites are never grabbed — see
:func:`ready_tasks` / :func:`waiting_tasks` for what is parallelizable vs blocked.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .event_types import EVENT_MODEL_CALL
from .events import append_event
from .models import Assignee, EventType, Task, TaskStatus, make_event
from .task_state import (
    DependencyCycle,
    assert_acyclic,
    assert_transition,
    is_terminal,
)

_TASK_COLUMNS = (
    "id, workstream, type, status, priority, assignee, payload, result, "
    "heartbeat_at, claimed_by, agent_type, claimed_at, depends_on, "
    "budget_tokens, spent_tokens, retries, last_progress_at, "
    "no_progress_rekicks, stall_reason, nudged_at, created_at, updated_at"
)

#: Columns a ``grab_task`` ``sort`` may order by. The ORDER BY is assembled ONLY
#: from these validated tokens (+ direction/nulls below) — caller input is NEVER
#: interpolated raw — so a value like ``(SELECT pg_sleep(3))`` can't become SQL.
_SORTABLE_COLUMNS = frozenset({
    "priority", "created_at", "updated_at", "claimed_at", "id",
    "workstream", "type", "agent_type",
})
_SORT_DIRECTIONS = frozenset({"ASC", "DESC"})
_SORT_NULLS = frozenset({"NULLS FIRST", "NULLS LAST"})

#: Default grab ordering: highest priority first, then oldest (FIFO within tier).
DEFAULT_SORT_TERMS: tuple[tuple[str, str], ...] = (("priority", "DESC"), ("created_at", "ASC"))

#: Columns a ``grab_task`` filter may constrain. Values are ALWAYS bound as ``%s``
#: parameters (or ``= ANY(%s)`` for a list) — never interpolated — so a hostile
#: filter value is treated as data, never executed. Adding a column here is the
#: only way to widen the surface; anything else raises ``ValueError``.
_FILTERABLE_COLUMNS = frozenset({
    "type", "workstream", "assignee", "priority", "agent_type", "claimed_by",
})

_UNSET = object()


def _parse_sort_term(term, alias: str) -> str:
    """Validate one sort term (str ``"col [ASC|DESC] [NULLS FIRST|LAST]"`` or a
    ``(col, direction[, nulls])`` tuple) into a safe ``alias.col DIR [NULLS …]``
    fragment built ONLY from allowlisted tokens. Raises ``ValueError`` otherwise."""
    if isinstance(term, (list, tuple)):
        parts = list(term)
        if not parts:
            raise ValueError("empty sort term")
        col = str(parts[0]).strip()
        direction = str(parts[1]).strip().upper() if len(parts) > 1 and parts[1] else "ASC"
        nulls = str(parts[2]).strip().upper() if len(parts) > 2 and parts[2] else None
    else:
        tokens = str(term).split()
        if not tokens:
            raise ValueError("empty sort term")
        col = tokens[0]
        direction, nulls, rest = "ASC", None, tokens[1:]
        if rest and rest[0].upper() in _SORT_DIRECTIONS:
            direction = rest.pop(0).upper()
        if rest:
            nulls = " ".join(rest).upper()  # must be exactly "NULLS FIRST"/"LAST"

    if col not in _SORTABLE_COLUMNS:
        raise ValueError(f"unsortable column {col!r} (allowed: {sorted(_SORTABLE_COLUMNS)})")
    if direction not in _SORT_DIRECTIONS:
        raise ValueError(f"invalid sort direction {direction!r} (ASC/DESC only)")
    frag = f"{alias}.{col} {direction}"
    if nulls is not None:
        if nulls not in _SORT_NULLS:
            raise ValueError(f"invalid nulls ordering {nulls!r} (NULLS FIRST/LAST only)")
        frag += f" {nulls}"
    return frag


def _build_sort(sort, alias: str = "t") -> str:
    """Build a safe ORDER-BY clause from a structured ``sort`` (allowlist only).

    ``sort`` may be ``None`` (→ the default priority DESC, created_at ASC), a
    comma-separated string (each term parsed + validated), or a list of terms
    (strings or ``(col, direction[, nulls])`` tuples). The result is composed
    entirely of allowlisted column/direction/nulls tokens — caller input is never
    interpolated — so it cannot carry a subquery/function call/second statement.
    """
    if not sort:
        terms: list = list(DEFAULT_SORT_TERMS)
    elif isinstance(sort, str):
        terms = [t for t in sort.split(",") if t.strip()]
    elif isinstance(sort, (list, tuple)):
        terms = list(sort)
    else:
        raise TypeError("sort must be None, a str, or a list of terms")
    frags = [_parse_sort_term(t, alias) for t in terms]
    if not frags:
        raise ValueError("empty sort expression")
    return ", ".join(frags)


def _build_filter(filter: Optional[dict], alias: str = "t") -> tuple[list[str], list[object]]:
    """Turn a structured ``{column: value}`` filter into parameterized SQL.

    Each column must be in :data:`_FILTERABLE_COLUMNS` (else ``ValueError``); each
    value is BOUND as a ``%s`` parameter (a list/tuple/set becomes ``= ANY(%s)``),
    so a value like ``"x'; DROP TABLE tasks;--"`` is compared as a literal string
    and can never inject SQL. Returns ``(clauses, params)`` to AND into the query.
    """
    clauses: list[str] = []
    params: list[object] = []
    if not filter:
        return clauses, params
    if not isinstance(filter, dict):
        raise TypeError("grab_task filter must be a mapping of column -> value")
    for col, val in filter.items():
        if col not in _FILTERABLE_COLUMNS:
            raise ValueError(
                f"unfilterable column {col!r} (allowed: {sorted(_FILTERABLE_COLUMNS)})"
            )
        if isinstance(val, (list, tuple, set)):
            clauses.append(f"{alias}.{col} = ANY(%s)")
            params.append(list(val))
        else:
            clauses.append(f"{alias}.{col} = %s")
            params.append(val)
    return clauses, params


#: Trust states that quarantine an identity from claiming any work (ADR-0021).
#: A ``revoked`` identity (permanently, after a fabrication) or a ``quarantined``
#: one is fenced out of the task queue as well as the human-relay path — untrusted
#: output must not re-enter the studio under a new task.
_QUARANTINE_TRUST_STATES: frozenset[str] = frozenset({"revoked", "quarantined"})


def _identity_quarantined(cur: psycopg.Cursor, identity: Optional[str]) -> bool:
    """True iff ``identity`` is quarantined/revoked in the trust ledger.

    A plain, side-effect-free READ of ``identity_trust`` (it does NOT auto-create a
    row, unlike :func:`runtime.trust.get_trust`), so it is behavior-preserving for
    every identity with no ledger row — first contact is trusted by default and
    claims normally. Only an identity that has EARNED a ``revoked``/``quarantined``
    state is fenced out. Takes the CALLER's open cursor so the read runs inside the
    grab's own transaction (never opening a stray one that would poison the enclosing
    ``with conn.transaction()``). Kept as a direct query (no import of
    ``runtime.trust``) so the grab path takes on no new module dependency.
    """
    if not identity:
        return False
    cur.execute(
        "SELECT trust_state FROM identity_trust WHERE identity = %s", (identity,)
    )
    row = cur.fetchone()
    return row is not None and row["trust_state"] in _QUARANTINE_TRUST_STATES


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


# --- The single guarded transition ------------------------------------------


def transition(
    conn: psycopg.Connection,
    task_id: UUID,
    to: TaskStatus,
    *,
    agent_id: Optional[str] = None,
    agent_type: Optional[str] = None,
    expected_from: Optional[TaskStatus] = None,
    result: Any = _UNSET,
    spent_tokens: Optional[int] = None,
    claimed_by: Any = _UNSET,
    clear_claim: bool = False,
    set_claimed_at: bool = False,
    set_heartbeat: bool = False,
    increment_retries: bool = False,
    set_last_progress: bool = False,
    sink: Any = None,
    force: bool = False,
) -> Optional[Task]:
    """Move ``task_id`` to state ``to`` — THE single guarded state change.

    Legal moves are defined by :mod:`runtime.task_state`; an illegal move raises
    :class:`~runtime.task_state.IllegalTransition` (unless ``force``). The UPDATE
    is guarded on the current status (so a concurrent change is a no-op → returns
    ``None``); ``expected_from`` additionally requires a specific source state.

    Records a ``task_transitions`` row (with ``latency_ms`` since this task's
    previous transition) and emits a ``task.transition`` event carrying only
    ids/statuses/agent/latency — never secret text. The optional column setters
    (``result``, ``spent_tokens``, ``claimed_by``/``clear_claim``,
    ``set_claimed_at``, ``set_heartbeat``, ``increment_retries``,
    ``set_last_progress``) let one guarded write also carry the bookkeeping a given
    transition needs.
    """
    to_val = to.value if isinstance(to, TaskStatus) else str(to)
    exp_val = (
        expected_from.value if isinstance(expected_from, TaskStatus) else expected_from
    )

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,)
            )
            row = cur.fetchone()
            if row is None:
                return None
            current = row["status"]
            if exp_val is not None and current != exp_val:
                return None  # guarded: not in the expected source state
            if current == to_val:
                return None  # idempotent no-op (already there)
            if not force:
                assert_transition(current, to_val)  # raises IllegalTransition

            sets = ["status = %s", "updated_at = now()"]
            params: list[object] = [to_val]
            if result is not _UNSET:
                sets.append("result = %s")
                params.append(Jsonb(result) if result is not None else None)
            if spent_tokens is not None:
                sets.append("spent_tokens = COALESCE(%s, spent_tokens)")
                params.append(spent_tokens)
            if claimed_by is not _UNSET:
                sets.append("claimed_by = %s")
                params.append(claimed_by)
            if agent_type is not None:
                sets.append("agent_type = %s")
                params.append(agent_type)
            if set_claimed_at:
                sets.append("claimed_at = now()")
            if set_heartbeat:
                sets.append("heartbeat_at = now()")
            if clear_claim:
                sets.append("claimed_by = NULL")
                sets.append("heartbeat_at = NULL")
            if increment_retries:
                sets.append("retries = retries + 1")
            if set_last_progress:
                # Baseline the progress watermark (ADR-0023): work is (re)starting,
                # so net progress is measured from now forward.
                sets.append("last_progress_at = now()")

            params.extend([task_id, current])
            cur.execute(
                f"""
                UPDATE tasks SET {', '.join(sets)}
                WHERE id = %s AND status = %s
                RETURNING {_TASK_COLUMNS}
                """,
                params,
            )
            updated = cur.fetchone()
            if updated is None:
                return None
            task = Task.model_validate(updated)

            # Append-only lifecycle telemetry: latency since the previous
            # transition (or task creation for the first one).
            cur.execute(
                """
                INSERT INTO task_transitions
                    (task_id, from_status, to_status, agent_id, agent_type, latency_ms)
                VALUES (%s, %s, %s, %s, %s,
                    (EXTRACT(EPOCH FROM (now() - COALESCE(
                        (SELECT max(at) FROM task_transitions WHERE task_id = %s),
                        (SELECT created_at FROM tasks WHERE id = %s)
                    ))) * 1000)::bigint)
                RETURNING latency_ms
                """,
                (task_id, current, to_val, agent_id, agent_type or task.agent_type,
                 task_id, task_id),
            )
            latency_ms = cur.fetchone()["latency_ms"]

        _emit(
            conn, task, EventType.TASK_TRANSITION,
            **{"from": current, "to": to_val, "agent_id": agent_id,
               "agent_type": agent_type or task.agent_type, "latency_ms": latency_ms},
        )
        # Uniform terminal marker for existing consumers/telemetry: any transition
        # into a terminal state (merged/abandoned) also emits task.finished.
        if is_terminal(to_val):
            _emit(conn, task, EventType.TASK_FINISHED, spent_tokens=task.spent_tokens)
    return task


# --- Enqueue (with dependency edges) ----------------------------------------


def enqueue_task(
    conn: psycopg.Connection,
    *,
    workstream: str,
    type: str,
    payload: Optional[dict] = None,
    priority: int = 0,
    assignee: Optional[Assignee] = None,
    budget_tokens: Optional[int] = None,
    depends_on: Optional[list[UUID]] = None,
    trajectory_id: Optional[UUID] = None,
) -> Task:
    """Create an ``up_for_grabs`` task and emit ``task.created`` (ADR-0009/0015).

    Enqueueing is how a role/agent is "spawned"; the runtime later grabs it.
    ``depends_on`` lists prerequisite task ids — the task becomes grabbable only
    once every prerequisite is ``merged`` (see :func:`grab_task`). Self-dependency
    is rejected (:class:`~runtime.task_state.DependencyCycle`).

    ``trajectory_id`` optionally stamps the reasoning trajectory this task was born
    from (ADR-0020): when the PM decomposes a plan it links every created task back
    to the ``pm`` trajectory that decided it, so an outcome can later be attributed
    to the decision (``SELECT ... FROM tasks WHERE trajectory_id = …``). The link is
    set through this single guarded writer only — there is no ad-hoc UPDATE of the
    column anywhere (mirrors the transition-only discipline).
    """
    deps = list(depends_on or [])
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO tasks
                    (workstream, type, payload, priority, assignee, budget_tokens,
                     depends_on, trajectory_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    workstream,
                    type,
                    Jsonb(payload or {}),
                    priority,
                    assignee.value if assignee else None,
                    budget_tokens,
                    deps,
                    trajectory_id,
                ),
            )
            task = Task.model_validate(cur.fetchone())
            if task.id in task.depends_on:
                raise DependencyCycle(f"task {task.id} depends on itself")
        _emit(conn, task, EventType.TASK_CREATED, type=task.type, priority=task.priority)
    return task


# --- Read one task ----------------------------------------------------------


def get_task(conn: psycopg.Connection, task_id: UUID) -> Optional[Task]:
    """Read one task row by id (any status), or ``None`` if it does not exist.

    A plain, side-effect-free read used where a caller needs the FULL preserved
    row of a task it did not just transition — e.g. the PM reading the spec of a
    superseded (``abandoned``) stuck task to re-decompose it (ADR-0023, R2). Does
    not open a lingering transaction on a non-autocommit connection.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return Task.model_validate(row) if row is not None else None


# --- Grab / claim -----------------------------------------------------------


def grab_task(
    conn: psycopg.Connection,
    *,
    worker_id: str,
    agent_type: Optional[str] = None,
    assignee: Optional[Assignee] = None,
    sort: "str | list | None" = None,
    filter: Optional[dict] = None,
    workstream: Optional[str] = None,
) -> Optional[Task]:
    """Grab one grabbable ``up_for_grabs`` task and move it to ``claimed``.

    Grabbable = ``up_for_grabs`` AND every prerequisite is ``merged`` (so tasks
    with no unmet deps are independent + grabbable in parallel; dependents wait).
    Picks by the caller-supplied ``sort`` (default priority DESC, created_at ASC)
    with ``FOR UPDATE SKIP LOCKED`` so concurrent grabbers never take the same
    task. ``assignee`` targets host/offhost (or unassigned); ``filter`` is an
    optional **structured** ``{column: value}`` mapping (equality, ANDed; a list
    value becomes ``IN``) over an allowlist of columns — values are bound as
    parameters, never interpolated, so it is injection-safe (NOT raw SQL). On the
    grab it records ``claimed_by``/``agent_type``/``claimed_at`` + an initial
    heartbeat. Returns the claimed task, or ``None`` if nothing is grabbable.
    """
    order_by = _build_sort(sort)  # allowlist-parsed; never raw SQL
    filter_clauses, filter_params = _build_filter(filter)  # validates + parameterizes
    clauses = ["t.status = 'up_for_grabs'"]
    params: list[object] = []
    if assignee is not None:
        clauses.append("(t.assignee IS NULL OR t.assignee = %s)")
        params.append(assignee.value)
    if workstream is not None:
        clauses.append("t.workstream = %s")
        params.append(workstream)
    clauses.extend(filter_clauses)
    params.extend(filter_params)
    # Dependency gate: reject if ANY prerequisite is not yet merged (unmet or
    # abandoned) — such a task is not grabbable (see waiting_tasks()).
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM unnest(t.depends_on) AS dep(id) "
        "JOIN tasks p ON p.id = dep.id WHERE p.status <> 'merged')"
    )
    where = " AND ".join(clauses)

    with conn.transaction():
        with conn.cursor() as cur:
            # Quarantine gate (ADR-0021): a revoked/quarantined identity is fenced
            # out of the queue — its untrusted output must not re-enter the studio
            # as new work. Behavior-preserving for trusted/unknown identities (no
            # ledger row = trusted by default). Runs inside THIS transaction so it
            # never opens a stray one.
            if _identity_quarantined(cur, worker_id):
                return None
            cur.execute(
                f"""
                SELECT t.id FROM tasks t
                WHERE {where}
                ORDER BY {order_by}
                FOR UPDATE OF t SKIP LOCKED
                LIMIT 1
                """,
                params,
            )
            picked = cur.fetchone()
            if picked is None:
                return None
        task = transition(
            conn, picked["id"], TaskStatus.CLAIMED,
            agent_id=worker_id, agent_type=agent_type,
            expected_from=TaskStatus.UP_FOR_GRABS,
            claimed_by=worker_id, set_claimed_at=True, set_heartbeat=True,
        )
    return task


def start_task(
    conn: psycopg.Connection, task_id: UUID, worker_id: str,
    *, agent_type: Optional[str] = None,
) -> Optional[Task]:
    """Move a ``claimed`` task to ``in_progress`` (work begins); refresh heartbeat.

    Also baselines the progress watermark (``last_progress_at = now``, ADR-0023):
    work is starting, so the supervisor measures NET progress for this attempt from
    here forward (model.call events + trajectory_steps newer than the watermark).
    """
    return transition(
        conn, task_id, TaskStatus.IN_PROGRESS,
        agent_id=worker_id, agent_type=agent_type,
        expected_from=TaskStatus.CLAIMED, set_heartbeat=True, set_last_progress=True,
    )


def claim_task(
    conn: psycopg.Connection,
    *,
    worker_id: str,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
    agent_type: Optional[str] = None,
    sort: "str | list | None" = None,
    filter: Optional[dict] = None,
) -> Optional[Task]:
    """Grab the next grabbable task and start it — the runtime loop's convenience.

    Equivalent to :func:`grab_task` (up_for_grabs→claimed) then :func:`start_task`
    (claimed→in_progress), returning the ``in_progress`` task ready to work, or
    ``None`` when nothing is grabbable. Preserves the historical ``claim_task``
    contract so the worker loop keeps working unchanged.
    """
    grabbed = grab_task(
        conn, worker_id=worker_id, agent_type=agent_type,
        assignee=assignee, sort=sort, filter=filter, workstream=workstream,
    )
    if grabbed is None:
        return None
    started = start_task(conn, grabbed.id, worker_id, agent_type=agent_type)
    return started or grabbed


# --- Heartbeat --------------------------------------------------------------


def heartbeat(
    conn: psycopg.Connection, task_id: UUID, worker_id: str
) -> Optional[Task]:
    """Refresh a task's heartbeat; returns the task, or None if not held.

    Only the worker holding the claim may heartbeat, and only while the task is
    actively held (``claimed`` or ``in_progress``). Emits **no** event: heartbeats
    are high-frequency liveness with zero replay value (ADR-0013).

    A heartbeat also CLEARS any open nudge episode (``nudged_at = NULL``, ADR-0023):
    the worker is alive again, so the cheap "nudge + grace" rung has done its job —
    the deferred re-kick is cancelled and its in-flight progress preserved. A
    heartbeat is deliberately NOT counted as progress itself (it is bare liveness; a
    task can heartbeat while making zero forward progress — the exact failure the
    progress detector must catch), so ``last_progress_at`` is left untouched here.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET heartbeat_at = now(), updated_at = now(), nudged_at = NULL
                WHERE id = %s AND claimed_by = %s
                  AND status IN ('claimed', 'in_progress')
                RETURNING {_TASK_COLUMNS}
                """,
                (task_id, worker_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


# --- Completion (terminal finalize for non-reviewed internal tasks) ---------


def complete_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    result: Optional[dict] = None,
    status: TaskStatus = TaskStatus.MERGED,
    spent_tokens: Optional[int] = None,
    force: bool = False,
) -> Optional[Task]:
    """Finalize an ``in_progress`` task to ``merged`` (success) or ``abandoned``.

    Used by the non-work handlers (pm.tick / retro / research / review) and the
    supervisor; ``work.*`` tasks reach ``merged`` through the review flow in the
    worker. ``status`` must be a terminal state (``MERGED``/``ABANDONED``; the
    legacy ``DONE``/``FAILED`` are not valid here). A success is driven through the
    canonical ``in_progress → ready_for_review → approved → merged`` path (an
    internal task is auto-approved), so its full lifecycle telemetry is recorded;
    an abandon is ``current → abandoned``.

    By default only an ``in_progress`` task is finalized (a worker cannot finalize
    a task it no longer owns); a conflict/missing task returns ``None`` with no
    event. ``force=True`` finalizes from whatever the current (non-terminal) state
    is (the supervisor force-abandoning a stale task); it still emits ``task.finished``.
    """
    if not is_terminal(status):
        raise ValueError("complete_task status must be 'merged' or 'abandoned'")

    if status == TaskStatus.MERGED:
        # Auto-approve + merge an internal task through the canonical path.
        t = transition(
            conn, task_id, TaskStatus.READY_FOR_REVIEW,
            expected_from=None if force else TaskStatus.IN_PROGRESS,
            result=result, spent_tokens=spent_tokens, force=force,
        )
        if t is None:
            return None
        t = transition(conn, task_id, TaskStatus.APPROVED)
        if t is None:
            return None
        task = transition(conn, task_id, TaskStatus.MERGED)
    else:  # ABANDONED — reachable from any non-terminal state
        task = transition(
            conn, task_id, TaskStatus.ABANDONED,
            expected_from=None if force else TaskStatus.IN_PROGRESS,
            result=result, spent_tokens=spent_tokens, force=force,
        )
    # transition() emits task.finished on the terminal hop, so nothing to add here.
    return task


# --- Approval block / requeue -----------------------------------------------


def block_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    approval_id: UUID,
    reason: str = "",
) -> Optional[Task]:
    """Park an ``in_progress`` task as ``blocked`` on a pending 🔴 approval.

    Stores ``approval_id`` in ``result`` so :func:`runtime.worker.resume_approved`
    can match the task to its approval. Guarded to ``in_progress`` (returns
    ``None`` otherwise). The ``task.transition`` event records the block.
    """
    return transition(
        conn, task_id, TaskStatus.BLOCKED,
        expected_from=TaskStatus.IN_PROGRESS,
        result={"blocked_on_approval": str(approval_id), "reason": reason},
    )


def requeue_blocked_task(conn: psycopg.Connection, task_id: UUID) -> Optional[Task]:
    """Re-queue a ``blocked`` task (→ ``up_for_grabs``) once its approval is granted.

    Clears the claim so a fresh worker re-grabs and re-runs the action — on that
    retry :func:`runtime.enforce.invoke` finds the live grant and executes. Guarded
    to ``blocked``.
    """
    return transition(
        conn, task_id, TaskStatus.UP_FOR_GRABS,
        expected_from=TaskStatus.BLOCKED, clear_claim=True,
    )


def find_blocked_tasks(conn: psycopg.Connection) -> list[Task]:
    """Return all tasks currently parked ``blocked`` on an approval (oldest first)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE status = 'blocked' ORDER BY created_at ASC"
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


# --- Reviewer helpers -------------------------------------------------------


def list_for_review(
    conn: psycopg.Connection, *, workstream: Optional[str] = None, limit: Optional[int] = None
) -> list[Task]:
    """Tasks awaiting review (``ready_for_review``), oldest first.

    A human / off-host Reviewer queries this, then drives each one
    ``ready_for_review → approved`` (then merged) or ``→ reviewer_blocked`` via
    :func:`transition`.
    """
    clauses = ["status = 'ready_for_review'"]
    params: list[object] = []
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    sql = (
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE {' AND '.join(clauses)} "
        "ORDER BY priority DESC, created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


# --- Dependency visibility (what's parallelizable vs blocked) ---------------


def ready_tasks(
    conn: psycopg.Connection,
    *,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Task]:
    """``up_for_grabs`` tasks with ALL prerequisites merged — grabbable now.

    These have no unmet dependency, so they are independent and can be grabbed in
    parallel by the fleet. Ordered like the grab path (priority DESC, created_at).
    """
    clauses = ["t.status = 'up_for_grabs'"]
    params: list[object] = []
    if assignee is not None:
        clauses.append("(t.assignee IS NULL OR t.assignee = %s)")
        params.append(assignee.value)
    if workstream is not None:
        clauses.append("t.workstream = %s")
        params.append(workstream)
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM unnest(t.depends_on) AS dep(id) "
        "JOIN tasks p ON p.id = dep.id WHERE p.status <> 'merged')"
    )
    sql = (
        f"SELECT {_TASK_COLUMNS} FROM tasks t WHERE {' AND '.join(clauses)} "
        "ORDER BY t.priority DESC, t.created_at ASC"
    )
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


def waiting_tasks(
    conn: psycopg.Connection, *, workstream: Optional[str] = None
) -> list[dict]:
    """``up_for_grabs`` tasks blocked by an unmet/abandoned prerequisite.

    Returns one dict per waiting task: ``task`` (the :class:`Task`),
    ``pending_prereqs`` (prereq ids not yet merged), and ``blocked_by_abandoned``
    (True if any prerequisite is ``abandoned`` — the dependent can then never run
    and is surfaced here, never silently grabbed). Complements :func:`ready_tasks`.
    """
    clauses = ["t.status = 'up_for_grabs'", "cardinality(t.depends_on) > 0"]
    params: list[object] = []
    if workstream is not None:
        clauses.append("t.workstream = %s")
        params.append(workstream)
    clauses.append(
        "EXISTS (SELECT 1 FROM unnest(t.depends_on) AS dep(id) "
        "JOIN tasks p ON p.id = dep.id WHERE p.status <> 'merged')"
    )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks t WHERE {' AND '.join(clauses)} "
            "ORDER BY t.created_at ASC",
            params,
        )
        rows = cur.fetchall()
        out: list[dict] = []
        for r in rows:
            task = Task.model_validate(r)
            cur.execute(
                "SELECT id, status FROM tasks WHERE id = ANY(%s) AND status <> 'merged'",
                (task.depends_on,),
            )
            pend = cur.fetchall()
            out.append({
                "task": task,
                "pending_prereqs": [p["id"] for p in pend],
                "blocked_by_abandoned": any(p["status"] == "abandoned" for p in pend),
            })
    if not conn.autocommit:
        conn.commit()
    return out


# --- Telemetry --------------------------------------------------------------


def add_spent_tokens(
    conn: psycopg.Connection, task_id: UUID, tokens: int
) -> Optional[Task]:
    """Increment a task's ``spent_tokens`` by ``tokens`` (telemetry; ADR-0012).

    Called by the instrumented model-call wrapper after each LLM call so a task's
    cumulative token spend is tracked live. Does not change status; emits no event
    (the ``model.call`` event already carries per-call tokens + cost).
    """
    if tokens < 0:
        raise ValueError("tokens must be non-negative")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks
                SET spent_tokens = spent_tokens + %s, updated_at = now()
                WHERE id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (tokens, task_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def task_lifecycle(conn: psycopg.Connection, task_id: UUID) -> dict:
    """Ordered transitions + per-hop durations + total wall-clock for one task.

    Returns ``{"transitions": [...], "total_ms": int|None, "current": status|None,
    "depends_on": [...]}`` where each transition carries from/to/agent/agent_type/at
    and its ``latency_ms`` (time spent in the *previous* state). The full canonical
    journey of a task, straight from the append-only ``task_transitions`` table.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT from_status, to_status, agent_id, agent_type, at, latency_ms
            FROM task_transitions WHERE task_id = %s ORDER BY at ASC, id ASC
            """,
            (task_id,),
        )
        trans = cur.fetchall()
        cur.execute("SELECT status, depends_on FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    total = None
    if trans:
        total = sum(int(t["latency_ms"] or 0) for t in trans)
    return {
        "transitions": [dict(t) for t in trans],
        "total_ms": total,
        "current": row["status"] if row else None,
        "depends_on": list(row["depends_on"]) if row else [],
    }


def task_cost(conn: psycopg.Connection, task_id: UUID) -> dict:
    """Sum tokens + cost + latency from a task's ``model.call`` events (ADR-0012).

    Returns ``{"calls", "input_tokens", "output_tokens", "cached_tokens",
    "total_tokens", "cost_usd", "latency_ms", "spent_tokens"}`` — cost per task,
    linked because every model call on a task's behalf carries its ``task_id``.
    ``spent_tokens`` is the live counter on the task row (cross-check).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                count(*) AS calls,
                COALESCE(sum((payload->>'input_tokens')::bigint), 0)  AS input_tokens,
                COALESCE(sum((payload->>'output_tokens')::bigint), 0) AS output_tokens,
                COALESCE(sum((payload->>'cached_tokens')::bigint), 0) AS cached_tokens,
                COALESCE(sum((payload->>'cost_usd')::numeric), 0)     AS cost_usd,
                COALESCE(sum((payload->>'latency_ms')::bigint), 0)    AS latency_ms
            FROM events WHERE task_id = %s AND type = 'model.call'
            """,
            (task_id,),
        )
        agg = cur.fetchone()
        cur.execute("SELECT spent_tokens FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    out = {k: (int(v) if k != "cost_usd" else float(v)) for k, v in agg.items()}
    out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    out["spent_tokens"] = int(row["spent_tokens"]) if row else 0
    return out


def agent_rollup(conn: psycopg.Connection) -> list[dict]:
    """Per-(agent_type, to_status) transition counts + avg latency (ADR-0012)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT agent_type, to_status,
                   count(*) AS transitions,
                   COALESCE(avg(latency_ms), 0)::bigint AS avg_latency_ms
            FROM task_transitions
            GROUP BY agent_type, to_status
            ORDER BY agent_type NULLS FIRST, to_status
            """
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [dict(r) for r in rows]


def model_rollup(conn: psycopg.Connection) -> list[dict]:
    """Per-model call counts, avg latency, and total cost from ``model.call`` events."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'model' AS model,
                   count(*) AS calls,
                   COALESCE(avg((payload->>'latency_ms')::bigint), 0)::bigint AS avg_latency_ms,
                   COALESCE(sum((payload->>'cost_usd')::numeric), 0)::float8 AS cost_usd,
                   COALESCE(sum((payload->>'input_tokens')::bigint
                              + (payload->>'output_tokens')::bigint), 0) AS total_tokens
            FROM events WHERE type = 'model.call'
            GROUP BY payload->>'model'
            ORDER BY calls DESC
            """
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [dict(r) for r in rows]


# --- Supervisor recovery ----------------------------------------------------


def find_stale_tasks(
    conn: psycopg.Connection, threshold_seconds: float
) -> list[Task]:
    """Return actively-held tasks whose heartbeat is older than the threshold.

    Exactly what the non-agent supervisor (ADR-0004) polls to find silently-dropped
    tasks. ``claimed`` (grabbed, never started) and ``in_progress`` tasks both
    count; a null heartbeat also counts as stale. Oldest-heartbeat-first.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_TASK_COLUMNS} FROM tasks
            WHERE status IN ('claimed', 'in_progress')
              AND (heartbeat_at IS NULL
                   OR heartbeat_at < now() - make_interval(secs => %s))
            ORDER BY heartbeat_at ASC NULLS FIRST
            """,
            (float(threshold_seconds),),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Task.model_validate(r) for r in rows]


def task_made_progress(conn: psycopg.Connection, task_id: UUID) -> bool:
    """True iff a task made NET progress since its ``last_progress_at`` watermark.

    The deterministic progress signal for the graduated recovery ladder (ADR-0023).
    "Progress" = a real forward-work signal produced by THIS attempt, newer than the
    watermark set at :func:`start_task` (or advanced at the previous re-kick):

    - a ``model.call`` event for this task (an LLM actually ran — this is also the
      source of ``spent_tokens`` increments, so it subsumes a token-spend check), or
    - a ``trajectory_steps`` row for the task's linked reasoning trajectory (ADR-0020
      — a reasoning step was recorded).

    A bare heartbeat is deliberately NOT progress: a task can heartbeat while making
    zero forward progress, which is exactly the endless-reset failure this guards.
    When ``last_progress_at`` is NULL (no baseline) ANY such signal counts. Read-only.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_progress_at, trajectory_id FROM tasks WHERE id = %s",
            (task_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        since = row["last_progress_at"]
        trajectory_id = row["trajectory_id"]

        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM events WHERE task_id = %s AND type = %s "
            "AND (%s::timestamptz IS NULL OR ts > %s)) AS hit",
            (task_id, EVENT_MODEL_CALL, since, since),
        )
        made = bool(cur.fetchone()["hit"])
        if not made and trajectory_id is not None:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM trajectory_steps WHERE trajectory_id = %s "
                "AND (%s::timestamptz IS NULL OR created_at > %s)) AS hit",
                (trajectory_id, since, since),
            )
            made = bool(cur.fetchone()["hit"])
    if not conn.autocommit:
        conn.commit()
    return made


def nudge_task(
    conn: psycopg.Connection, task_id: UUID, *, grace_s: float
) -> Optional[Task]:
    """Issue a NUDGE for a stalled held task — the cheapest recovery rung (ADR-0023).

    Marks ``nudged_at = now`` (opening the current stall episode) WITHOUT changing
    status or clearing the claim, so the worker keeps its in-flight progress; the
    supervisor then defers the re-kick for ``grace_s`` so a transient stall can
    recover (a heartbeat clears ``nudged_at``; see :func:`heartbeat`). Guarded to a
    still-held (``claimed``/``in_progress``) task with NO open nudge episode — a
    task that recovered/changed state or is already nudged is left untouched
    (``None``). Emits a body-free ``task.nudge`` (status + grace only).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE tasks SET nudged_at = now(), updated_at = now()
                WHERE id = %s AND status IN ('claimed', 'in_progress')
                  AND nudged_at IS NULL
                RETURNING {_TASK_COLUMNS}
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            task = Task.model_validate(row)
        _emit(conn, task, EventType.TASK_NUDGE, grace_s=float(grace_s))
    return task


def rekick_task(
    conn: psycopg.Connection, task_id: UUID, *, made_progress: Optional[bool] = None
) -> Optional[Task]:
    """Re-queue a stale held task (→ ``up_for_grabs``) for a fresh worker; ``task.rekicked``.

    The non-agent supervisor's core recovery (ADR-0004): a task whose worker went
    silent is returned to the grab pool with its claim/heartbeat cleared and
    ``retries`` incremented. Handles both ``claimed`` and ``in_progress`` (the
    latter via the documented in_progress→up_for_grabs recovery edge). Guarded to
    those two states — a task that changed state is left untouched (``None``).

    Progress-aware (ADR-0023): pass ``made_progress`` (measured by
    :func:`task_made_progress` for the attempt just ending) to maintain the
    ``no_progress_rekicks`` counter atomically with the re-kick — reset to 0 when
    progress was seen, incremented when NOT. Either way the nudge episode is closed
    (``nudged_at = NULL``) and the progress watermark advanced (``last_progress_at =
    now``) so the NEXT attempt is measured from here. ``made_progress=None`` (the
    default, for callers with no signal) leaves the counter/watermark untouched,
    preserving the pre-ADR-0023 behavior.
    """
    with conn.transaction():
        # Re-kick ONLY an actively-held task (claimed/in_progress) — the documented
        # contract, and all the supervisor ever feeds here (find_stale_tasks filters
        # to those two states). Locking + checking the row FIRST closes the park
        # race: the state machine also permits blocked→up_for_grabs, so without this
        # guard a re-kick could clobber a task that was concurrently PARKED `blocked`
        # by request_decision — stranding an `open` decision against a runnable task.
        # The FOR UPDATE lock held for this transaction serializes against the park:
        # either the park committed first (status is now `blocked` → we bail, leaving
        # it parked) or we win (status still in_progress → the park then aborts).
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
            row = cur.fetchone()
        if row is None or row["status"] not in (
            TaskStatus.CLAIMED.value, TaskStatus.IN_PROGRESS.value
        ):
            return None
        task = transition(
            conn, task_id, TaskStatus.UP_FOR_GRABS,
            clear_claim=True, increment_retries=True,
        )
        if task is None or task.status is not TaskStatus.UP_FOR_GRABS:
            return None
        no_progress_rekicks = task.no_progress_rekicks
        if made_progress is not None:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE tasks SET
                        no_progress_rekicks = CASE WHEN %s THEN 0
                                                   ELSE no_progress_rekicks + 1 END,
                        nudged_at = NULL,
                        last_progress_at = now(),
                        updated_at = now()
                    WHERE id = %s
                    RETURNING no_progress_rekicks
                    """,
                    (made_progress, task_id),
                )
                no_progress_rekicks = int(cur.fetchone()["no_progress_rekicks"])
        _emit(
            conn, task, EventType.TASK_REKICKED, retries=task.retries,
            made_progress=made_progress, no_progress_rekicks=no_progress_rekicks,
        )
    return task


def escalate_stuck_task(
    conn: psycopg.Connection,
    task_id: UUID,
    *,
    stall_reason: str = "no_progress",
    no_progress_rekicks: int = 0,
    retries: int = 0,
) -> Optional[Task]:
    """Escalate a stuck task to the PM and supersede the attempt (ADR-0023, R1 signal).

    The progress-aware bail-out: re-kicks made NO net progress up to the stuck
    threshold, so instead of resetting forever the supervisor STOPS, records the
    ``stall_reason`` code, emits a body-free ``task.stuck`` SIGNAL (reason + counts —
    the input R2's PM consumes to re-decompose into smaller subtasks), and supersedes
    the attempt via ``complete_task(ABANDONED, result={"reason":
    "stuck_needs_replan", ...})``. This module emits ONLY the signal + supersede — it
    does NOT enqueue the PM task (that is R2).

    Guarded like :func:`runtime.supervisor._default_fail_exhausted`: the abandon is
    NOT forced, so a task that self-completed in the scan→write window is left
    untouched and this returns ``None`` (the sweep logs a skip). ``stall_reason`` is
    a short CODE, never body text. Runs the reason-write, the abandon, and the event
    in one transaction so they commit atomically.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET stall_reason = %s, updated_at = now() "
                "WHERE id = %s AND status = 'in_progress'",
                (stall_reason, task_id),
            )
            if cur.rowcount == 0:
                return None  # no longer in_progress (self-completed in the window)
        superseded = complete_task(
            conn,
            task_id,
            status=TaskStatus.ABANDONED,
            result={
                "reason": "stuck_needs_replan",
                "stall_reason": stall_reason,
                "no_progress_rekicks": no_progress_rekicks,
                "retries": retries,
            },
        )
        if superseded is None:
            return None
        _emit(
            conn, superseded, EventType.TASK_STUCK,
            stall_reason=stall_reason,
            no_progress_rekicks=no_progress_rekicks,
            retries=retries,
        )
    return superseded
