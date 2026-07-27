"""Approval-gated specialist handoffs relayed by Spokesman (ADR-0026).

Other agents never get raw channel credentials. Human must approve a
``handoff.propose`` approval before Spokesman relays tagged ``[Role]`` messages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from runtime.approvals import request_approval
from runtime.enforce import DbEventSink, EventSink
from runtime.event_types import (
    EVENT_HANDOFF_ACTIVATED,
    EVENT_HANDOFF_ENDED,
    EVENT_HANDOFF_PROPOSED,
)
from runtime.models import make_event

logger = logging.getLogger(__name__)

HANDOFF_TOOL = "handoff.propose"
DEFAULT_HANDOFF_HOURS = 4
STATUS_PROPOSED = "proposed"
STATUS_ACTIVE = "active"
STATUS_ENDED = "ended"


@dataclass(frozen=True)
class Handoff:
    id: UUID
    approval_id: Optional[UUID]
    role: str
    status: str
    workstream: str
    reason: str
    expires_at: Optional[datetime]


def _row_to_handoff(row: dict) -> Handoff:
    return Handoff(
        id=row["id"],
        approval_id=row.get("approval_id"),
        role=str(row["role"]),
        status=str(row["status"]),
        workstream=str(row.get("workstream") or "productivity"),
        reason=str(row.get("reason") or ""),
        expires_at=row.get("expires_at"),
    )


def propose_handoff(
    conn: psycopg.Connection,
    *,
    role: str,
    reason: str,
    workstream: str = "productivity",
    sink: Optional[EventSink] = None,
    hours: int = DEFAULT_HANDOFF_HOURS,
) -> tuple[Handoff, Any]:
    """Create a proposed handoff + 🔴 approval the human must approve."""
    sink = sink or DbEventSink(conn)
    role_norm = (role or "pm").strip().lower() or "pm"
    reason_clean = (reason or "Specialist back-and-forth needed.").strip()[:500]
    approval = request_approval(
        conn,
        task_id=None,
        role="spokesman",
        tool=HANDOFF_TOOL,
        capabilities=[],
        tier="red",
        reason=f"Handoff to {role_norm}: {reason_clean[:200]}",
        sink=sink,
        workstream=workstream,
        args={"handoff_role": role_norm},
    )
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO spokesman_handoffs
              (approval_id, role, status, workstream, reason, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, approval_id, role, status, workstream, reason, expires_at
            """,
            (
                approval.id,
                role_norm,
                STATUS_PROPOSED,
                workstream,
                reason_clean,
                expires,
            ),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    handoff = _row_to_handoff(row)
    sink.emit(
        make_event(
            type=EVENT_HANDOFF_PROPOSED,
            workstream=workstream,
            payload={
                "handoff_id": str(handoff.id),
                "approval_id": str(approval.id),
                "role": role_norm,
            },
        )
    )
    return handoff, approval


def active_handoff(
    conn: psycopg.Connection,
    *,
    workstream: Optional[str] = None,
) -> Optional[Handoff]:
    """Return the current active (non-expired) handoff, if any."""
    clauses = ["status = %s", "(expires_at IS NULL OR expires_at > now())"]
    params: list[Any] = [STATUS_ACTIVE]
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, approval_id, role, status, workstream, reason, expires_at
                FROM spokesman_handoffs
                WHERE {' AND '.join(clauses)}
                ORDER BY activated_at DESC NULLS LAST
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        logger.warning("active_handoff query failed")
        return None
    if not row:
        return None
    return _row_to_handoff(row)


def activate_handoff_for_approval(
    conn: psycopg.Connection,
    approval_id: UUID | str,
    *,
    sink: Optional[EventSink] = None,
) -> Optional[Handoff]:
    """Mark the handoff linked to this approval as active (after human approve)."""
    sink = sink or DbEventSink(conn)
    if isinstance(approval_id, str):
        approval_id = UUID(approval_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE spokesman_handoffs
               SET status = %s, activated_at = now()
             WHERE approval_id = %s AND status = %s
         RETURNING id, approval_id, role, status, workstream, reason, expires_at
            """,
            (STATUS_ACTIVE, approval_id, STATUS_PROPOSED),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    if not row:
        return None
    handoff = _row_to_handoff(row)
    sink.emit(
        make_event(
            type=EVENT_HANDOFF_ACTIVATED,
            workstream=handoff.workstream,
            payload={
                "handoff_id": str(handoff.id),
                "approval_id": str(approval_id),
                "role": handoff.role,
            },
        )
    )
    return handoff


def end_handoff(
    conn: psycopg.Connection,
    handoff_id: UUID | str | None = None,
    *,
    sink: Optional[EventSink] = None,
) -> Optional[Handoff]:
    """End an active handoff (by id, or the current active one)."""
    sink = sink or DbEventSink(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        if handoff_id is not None:
            if isinstance(handoff_id, str):
                handoff_id = UUID(handoff_id)
            cur.execute(
                """
                UPDATE spokesman_handoffs
                   SET status = %s, ended_at = now()
                 WHERE id = %s AND status = %s
             RETURNING id, approval_id, role, status, workstream, reason, expires_at
                """,
                (STATUS_ENDED, handoff_id, STATUS_ACTIVE),
            )
        else:
            cur.execute(
                """
                UPDATE spokesman_handoffs
                   SET status = %s, ended_at = now()
                 WHERE status = %s
             RETURNING id, approval_id, role, status, workstream, reason, expires_at
                """,
                (STATUS_ENDED, STATUS_ACTIVE),
            )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    if not row:
        return None
    handoff = _row_to_handoff(row)
    sink.emit(
        make_event(
            type=EVENT_HANDOFF_ENDED,
            workstream=handoff.workstream,
            payload={"handoff_id": str(handoff.id), "role": handoff.role},
        )
    )
    return handoff


def format_handoff_relay(role: str, text: str) -> str:
    """Prefix specialist text for the shared channel."""
    tag = (role or "agent").strip().upper() or "AGENT"
    return f"[{tag}] {text}"
