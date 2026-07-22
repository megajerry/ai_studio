"""Approval store — the human-in-the-loop grant loop for 🔴 actions (M2 gap).

The policy engine (:mod:`runtime.policy`) decides a 🔴 / over-budget call is
``NEEDS_APPROVAL``; :func:`runtime.enforce.invoke` turns that into a **pending**
row here and refuses to run the tool. A human resolves it (approve / deny); an
approved row is a one-shot **grant** that authorizes exactly ONE execution and is
then marked ``consumed``. This is the persistence + state machine behind
architecture §5's 🔴 tier and ADR-0006's 🛑 "approve (blocks)" class.

Lifecycle of one action::

    invoke(🔴) → request_approval → [pending]
              → human: resolve_approval(approved) → [approved]  (a grant)
              → worker retries → invoke finds grant (find_grant)
                 → execute tool → consume_grant → [consumed]

    resolve_approval(denied) → [denied]  → the blocked task is failed.

`request_fingerprint` is a stable hash of ``(task_id, tool, sorted capabilities)``
so a later retry of the *same* action matches its grant. It contains NO argument
values or secrets (CLAUDE.md invariant 5) — only the action's identity.

All functions take an open ``conn`` (the caller owns the transaction boundary,
matching :mod:`runtime.tasks`) and an :class:`EventSink` for observability
(invariant 6). Pure helpers (:func:`compute_fingerprint`) need no database.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Optional
from uuid import UUID

import psycopg
from pydantic import BaseModel, Field

from .models import make_event

if TYPE_CHECKING:  # avoid a runtime import cycle (enforce imports approvals)
    from .enforce import EventSink

# Canonical event types for the approval loop. Owned here; re-exported by
# :mod:`runtime.enforce` so the enforcement path and this store agree on wires.
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_RESOLVED = "approval.resolved"

#: Terminal + transient statuses an approval row may hold.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_CONSUMED = "consumed"

_COLUMNS = (
    "id, task_id, role, tool, capabilities, tier, reason, request_fingerprint, "
    "status, created_at, resolved_at, resolver"
)


class Approval(BaseModel):
    """A persisted approval row (a 🔴 action awaiting / holding a human decision)."""

    id: UUID
    task_id: Optional[UUID] = None
    role: str
    tool: str
    capabilities: list[str] = Field(default_factory=list)
    tier: str
    reason: str = ""
    request_fingerprint: str
    status: str
    created_at: datetime
    resolved_at: Optional[datetime] = None
    resolver: Optional[str] = None


class ApprovalDigest(BaseModel):
    """A batched summary of pending approvals for the Spokesman (ADR-0006).

    Approvals are batched into a periodic digest by default; this is the shape
    the future Spokesman renders. Carries only action identity — no arg values.
    """

    count: int
    by_tier: dict[str, int] = Field(default_factory=dict)
    items: list[Approval] = Field(default_factory=list)


def compute_fingerprint(
    task_id: Optional[UUID], tool: str, capabilities: Iterable[str]
) -> str:
    """Stable hash identifying a 🔴 action so a grant can be matched to a retry.

    Pure and deterministic: the same ``(task_id, tool, capabilities)`` always
    yields the same fingerprint, so the first (pending) invocation and a later
    (post-approval) retry of the *same* task+tool+capabilities collide on it —
    which is how :func:`find_grant` re-attaches the grant. Capability order is
    normalized (sorted) so it never depends on set iteration order. No argument
    values enter the hash (invariant 5).
    """
    caps = ",".join(sorted(capabilities))
    material = "|".join([str(task_id or ""), tool, caps])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _emit(
    sink: "EventSink", *, type: str, approval: Approval, workstream: str
) -> None:
    """Emit an approval.* event carrying ids/role/tool/tier/reason — never args."""
    sink.emit(
        make_event(
            workstream=workstream,
            type=type,
            task_id=approval.task_id,
            payload={
                "approval_id": str(approval.id),
                "task_id": str(approval.task_id) if approval.task_id else None,
                "role": approval.role,
                "tool": approval.tool,
                "tier": approval.tier,
                "reason": approval.reason,
                "capabilities": approval.capabilities,
                "status": approval.status,
                "resolver": approval.resolver,
            },
        )
    )


def request_approval(
    conn: psycopg.Connection,
    *,
    task_id: Optional[UUID],
    role: str,
    tool: str,
    capabilities: Iterable[str],
    tier: str,
    reason: str,
    sink: "EventSink",
    workstream: str = "productivity",
    fingerprint: Optional[str] = None,
) -> Approval:
    """Create a ``pending`` approval row and emit ``approval.requested``.

    Idempotent per fingerprint: if an un-resolved (``pending``) or granted
    (``approved``) row already exists for the same action, it is returned as-is
    and NO duplicate row or event is created. Consumed/denied rows do not block a
    fresh request — a re-run after a grant was spent legitimately pends again.
    """
    caps = sorted(capabilities)
    fp = fingerprint or compute_fingerprint(task_id, tool, caps)

    with conn.transaction():
        with conn.cursor() as cur:
            # Reuse an existing open grant/request for this action (idempotency).
            cur.execute(
                f"""
                SELECT {_COLUMNS} FROM approvals
                WHERE request_fingerprint = %s AND status IN ('pending', 'approved')
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE
                """,
                (fp,),
            )
            existing = cur.fetchone()
            if existing is not None:
                return Approval.model_validate(existing)

            cur.execute(
                f"""
                INSERT INTO approvals
                    (task_id, role, tool, capabilities, tier, reason, request_fingerprint, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                RETURNING {_COLUMNS}
                """,
                (task_id, role, tool, caps, tier, reason, fp),
            )
            approval = Approval.model_validate(cur.fetchone())
        _emit(sink, type=EVENT_APPROVAL_REQUESTED, approval=approval, workstream=workstream)
    return approval


def resolve_approval(
    conn: psycopg.Connection,
    approval_id: UUID,
    decision: str,
    resolver: str,
    sink: "EventSink",
    workstream: str = "productivity",
) -> Optional[Approval]:
    """Resolve a pending approval to ``approved`` or ``denied``; emit ``approval.resolved``.

    Guarded to ``pending`` so an already-resolved (or consumed) approval is never
    re-decided: on such a conflict (or a missing id) nothing changes, no event is
    emitted, and ``None`` is returned. An ``approved`` row is now a one-shot grant
    (see :func:`find_grant` / :func:`consume_grant`); a ``denied`` row stays denied.
    """
    if decision not in (STATUS_APPROVED, STATUS_DENIED):
        raise ValueError("decision must be 'approved' or 'denied'")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE approvals
                SET status = %s, resolved_at = now(), resolver = %s
                WHERE id = %s AND status = 'pending'
                RETURNING {_COLUMNS}
                """,
                (decision, resolver, approval_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            approval = Approval.model_validate(row)
        _emit(sink, type=EVENT_APPROVAL_RESOLVED, approval=approval, workstream=workstream)
    return approval


def get_approval(conn: psycopg.Connection, approval_id: UUID) -> Optional[Approval]:
    """Fetch one approval by id regardless of status, or ``None`` if absent."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM approvals WHERE id = %s", (approval_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    if row is None:
        return None
    return Approval.model_validate(row)


def find_grant(conn: psycopg.Connection, fingerprint: str) -> Optional[Approval]:
    """Return an ``approved`` (not-yet-consumed) grant matching ``fingerprint``, or None.

    This is the check :func:`runtime.enforce.invoke` runs *before* pending a 🔴
    action: a live grant turns NEEDS_APPROVAL into an ALLOW for exactly one run.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS} FROM approvals
            WHERE request_fingerprint = %s AND status = 'approved'
            ORDER BY resolved_at ASC NULLS LAST, created_at ASC
            LIMIT 1
            """,
            (fingerprint,),
        )
        row = cur.fetchone()
    # Close the read's implicit transaction on a non-autocommit conn.
    if not conn.autocommit:
        conn.commit()
    if row is None:
        return None
    return Approval.model_validate(row)


def consume_grant(conn: psycopg.Connection, approval_id: UUID) -> Optional[Approval]:
    """Mark an ``approved`` grant ``consumed`` (one-shot). Returns it, or None.

    Guarded to ``status = 'approved'`` so a grant authorizes at most ONE
    execution: a second consume (or a race between two workers) matches nothing
    and returns ``None``. This is what makes a grant one-shot — a subsequent
    identical call finds no grant and pends afresh.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE approvals
                SET status = 'consumed'
                WHERE id = %s AND status = 'approved'
                RETURNING {_COLUMNS}
                """,
                (approval_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return Approval.model_validate(row)


def pending_approvals(conn: psycopg.Connection) -> list[Approval]:
    """List all currently-``pending`` approvals, oldest first (for the Spokesman)."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM approvals WHERE status = 'pending' ORDER BY created_at ASC"
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Approval.model_validate(r) for r in rows]


def pending_digest(conn: psycopg.Connection) -> ApprovalDigest:
    """Batch pending approvals into a digest (ADR-0006: approvals are batched).

    The future Spokesman renders this into the periodic 🛑 digest. Grouped by
    tier with a total count; carries only action identity, no argument values.
    """
    items = pending_approvals(conn)
    by_tier: dict[str, int] = {}
    for a in items:
        by_tier[a.tier] = by_tier.get(a.tier, 0) + 1
    return ApprovalDigest(count=len(items), by_tier=by_tier, items=items)
