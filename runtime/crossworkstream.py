"""Cross-workstream request contract — the second half of workstream-bootstrap.

Verticals (workstreams) are scope-isolated (ADR-0018) and coordinate **only**
through the shared task board + append-only event log — never by direct calls
(CLAUDE.md invariants 1 & 2). This module is how one workstream asks another to
build something: a typed :class:`FeatureRequest` carried as an ordinary task
(``type="feature_request"``) placed on the **receiving** workstream's board.

Lifecycle of one request (the sub-status lives in the task payload + the
``request.*`` event stream, distinct from the task's own lifecycle status)::

    submit_request(A→B)      → [submitted]   request.submitted   (task up_for_grabs in B)
        receiving PM triage  → [under_review] request.under_review
          accept   → decompose success_criteria into up_for_grabs work items in B
                     + [accepted]  request.accepted   (request task → merged)
          decline  → [declined]   request.declined   (reason; NO work; task → abandoned)
          clarify  → [needs_clarification] request.needs_clarification (back to requester)
          escalate → [escalated]  request.escalated  + a 🛑 request_approval
                     (portfolio/resource decision — either side may escalate)

Every ``request.*`` event carries only **identity** — request id, from/to
workstreams, sub-status, and (for decisions) the decision + reason. It NEVER
carries the request bodies (problem / desired_capability / success_criteria) or
any secret/PII (CLAUDE.md invariant 5) — those live in the task payload, which is
scoped to the receiving workstream. The requester observes outcomes purely from
the ``request.*`` event stream (:func:`read_events`), never a direct call.

The receiving PM evaluates through **its own** success lens: pushback (decline)
is a first-class outcome, mirroring the PM confidence gate (ADR-0003). The intake
step itself lives in :mod:`runtime.roles.pm` (:func:`~runtime.roles.pm.triage_request`);
this module owns the contract + the board/event plumbing it calls.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .enforce import EventSink, NullEventSink
from .models import Task, make_event
from .tasks import enqueue_task as _enqueue_task

log = logging.getLogger("runtime.crossworkstream")

#: Task ``type`` a cross-workstream request rides on. A dedicated type so the
#: receiving PM (and only it) can list/triage requests addressed to its board.
FEATURE_REQUEST_TYPE = "feature_request"

#: The request sub-status (stored under this key in the task payload, and carried
#: as ``status`` on every ``request.*`` event). Distinct from the task's own
#: lifecycle status (:class:`runtime.models.TaskStatus`).
REQUEST_STATUS_KEY = "request_status"

#: The request sub-status values (the intake state machine).
STATUS_SUBMITTED = "submitted"
STATUS_UNDER_REVIEW = "under_review"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_ESCALATED = "escalated"

#: Terminal sub-statuses (the request has been decided one way).
REQUEST_TERMINAL = frozenset(
    {STATUS_ACCEPTED, STATUS_DECLINED, STATUS_ESCALATED}
)

#: Canonical ``request.*`` event types. Owned here; the receiving-PM intake in
#: :mod:`runtime.roles.pm` emits these so producer/consumer agree on the wire.
EVENT_REQUEST_SUBMITTED = "request.submitted"
EVENT_REQUEST_UNDER_REVIEW = "request.under_review"
EVENT_REQUEST_ACCEPTED = "request.accepted"
EVENT_REQUEST_DECLINED = "request.declined"
EVENT_REQUEST_NEEDS_CLARIFICATION = "request.needs_clarification"
EVENT_REQUEST_ESCALATED = "request.escalated"

#: sub-status → the event announcing that decision.
_STATUS_EVENT = {
    STATUS_UNDER_REVIEW: EVENT_REQUEST_UNDER_REVIEW,
    STATUS_ACCEPTED: EVENT_REQUEST_ACCEPTED,
    STATUS_DECLINED: EVENT_REQUEST_DECLINED,
    STATUS_NEEDS_CLARIFICATION: EVENT_REQUEST_NEEDS_CLARIFICATION,
    STATUS_ESCALATED: EVENT_REQUEST_ESCALATED,
}


# --- The typed contract ------------------------------------------------------


class FeatureRequest(BaseModel):
    """A typed ask from one workstream to another (the cross-workstream contract).

    The requester states the *problem* and the *capability* it needs, plus the
    ``success_criteria`` the receiving workstream's work items will be checked
    against if the request is accepted (they become those items' criteria — the
    requester defines "done", the receiver decides *how*). ``impact``/``priority``
    frame the portfolio trade-off the receiving PM (or an escalation) weighs.

    The bodies below live only in the task payload (scoped to the receiver); the
    ``request.*`` event stream carries none of them (invariant 5).
    """

    from_workstream: str
    to_workstream: str
    title: str
    problem: str
    desired_capability: str
    success_criteria: list[str] = Field(default_factory=list)
    impact: str = ""
    priority: int = 0
    deadline: Optional[str] = None
    context_refs: list[str] = Field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """The request body as a task payload (bodies included — payload is scoped)."""
        return self.model_dump()

    @classmethod
    def from_task(cls, task: Task) -> "FeatureRequest":
        """Reconstruct the typed request from a ``feature_request`` task's payload."""
        if task.type != FEATURE_REQUEST_TYPE:
            raise ValueError(
                f"task {task.id} is type {task.type!r}, not {FEATURE_REQUEST_TYPE!r}"
            )
        payload = dict(task.payload or {})
        payload.pop(REQUEST_STATUS_KEY, None)  # sub-status is not a contract field
        payload.pop("work_task_ids", None)
        payload.pop("decision_reason", None)
        return cls.model_validate(payload)


def request_status(task: Task) -> Optional[str]:
    """The current request sub-status recorded in a request task's payload."""
    return (task.payload or {}).get(REQUEST_STATUS_KEY)


# --- Event emission (identity only — never bodies) --------------------------


def emit_request_event(
    sink: EventSink,
    *,
    type: str,
    request_id: UUID,
    from_workstream: str,
    to_workstream: str,
    status: str,
    decision: Optional[str] = None,
    reason: str = "",
    **extra: Any,
) -> None:
    """Emit one ``request.*`` event carrying **identity only** (invariant 5).

    Never include the request bodies (problem / desired_capability /
    success_criteria) or any secret/PII. The event is scoped to the receiving
    workstream (``to_workstream``) so it appears on the board both sides watch.
    ``extra`` may add non-secret scalars (ids/counts) — callers pass work-item
    counts/ids and approval ids, never text bodies.
    """
    payload: dict[str, Any] = {
        "request_id": str(request_id),
        "from_workstream": from_workstream,
        "to_workstream": to_workstream,
        "status": status,
    }
    if decision is not None:
        payload["decision"] = decision
    if reason:
        payload["reason"] = reason
    payload.update(extra)
    sink.emit(
        make_event(
            workstream=to_workstream,
            type=type,
            task_id=request_id,
            payload=payload,
        )
    )


def set_request_status(
    conn: psycopg.Connection,
    task_id: UUID,
    status: str,
    *,
    reason: str = "",
    work_task_ids: Optional[list[str]] = None,
) -> None:
    """Record the request sub-status (and optional decision detail) in the payload.

    This mutates the request task's ``payload`` only — it does NOT touch the task's
    lifecycle ``status`` column (that stays the province of the single guarded
    :func:`runtime.tasks.transition`). The sub-status is the request's own state
    machine, tracked in payload + the ``request.*`` events.
    """
    patch: dict[str, Any] = {REQUEST_STATUS_KEY: status}
    if reason:
        patch["decision_reason"] = reason
    if work_task_ids is not None:
        patch["work_task_ids"] = work_task_ids
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET payload = payload || %s::jsonb, updated_at = now() "
                "WHERE id = %s",
                (Jsonb(patch), task_id),
            )


# --- Submit ------------------------------------------------------------------


def submit_request(
    conn: psycopg.Connection,
    *,
    request: Optional[FeatureRequest] = None,
    sink: Optional[EventSink] = None,
    enqueue: Any = _enqueue_task,
    **fields: Any,
) -> Task:
    """File a :class:`FeatureRequest` onto the **receiving** workstream's board.

    Creates an ``up_for_grabs`` task with ``type="feature_request"`` and
    ``workstream=to_workstream`` (so it is scoped to — and only triageable by —
    the receiver), carrying the request body + a ``submitted`` sub-status, then
    emits ``request.submitted`` (identity only). Returns the created request task.

    Pass either a built ``request=FeatureRequest(...)`` or the fields inline.
    """
    sink = sink or NullEventSink()
    if request is None:
        request = FeatureRequest.model_validate(fields)
    elif fields:
        raise ValueError("pass either request= or inline fields, not both")

    payload = request.to_payload()
    payload[REQUEST_STATUS_KEY] = STATUS_SUBMITTED
    task = enqueue(
        conn,
        workstream=request.to_workstream,
        type=FEATURE_REQUEST_TYPE,
        payload=payload,
        priority=request.priority,
    )
    emit_request_event(
        sink,
        type=EVENT_REQUEST_SUBMITTED,
        request_id=task.id,
        from_workstream=request.from_workstream,
        to_workstream=request.to_workstream,
        status=STATUS_SUBMITTED,
    )
    return task


# --- Read --------------------------------------------------------------------

_TASK_COLUMNS = (
    "id, workstream, type, status, priority, assignee, payload, result, "
    "heartbeat_at, claimed_by, agent_type, claimed_at, depends_on, "
    "budget_tokens, spent_tokens, retries, created_at, updated_at"
)


def list_requests(
    conn: psycopg.Connection,
    to_workstream: str,
    *,
    status: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Task]:
    """Feature requests addressed to ``to_workstream`` (newest first).

    Scope-respecting by construction: only ``feature_request`` tasks whose
    ``workstream`` is ``to_workstream`` are returned, so a request filed to B is
    never surfaced to (or processable by) A. ``status`` optionally filters on the
    request sub-status (e.g. ``submitted``) recorded in the payload.
    """
    clauses = ["type = %s", "workstream = %s"]
    params: list[Any] = [FEATURE_REQUEST_TYPE, to_workstream]
    if status is not None:
        clauses.append(f"payload->>'{REQUEST_STATUS_KEY}' = %s")
        params.append(status)
    sql = (
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC"
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


def get_request(conn: psycopg.Connection, request_id: UUID) -> Optional[Task]:
    """Fetch one request task by id, or ``None`` if it is not a request task."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = %s AND type = %s",
            (request_id, FEATURE_REQUEST_TYPE),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return Task.model_validate(row) if row else None
