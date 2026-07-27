"""Studio context + anticipatory prep cache for Spokesman (ADR-0026).

The cache is a **latency aid**. Grounding (ADR-0021) still verifies claims
against the live runtime DB before anything is sent to the human.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from runtime.decisions import open_decisions
from runtime.models import make_event

from .runtime_bridge import DashboardSnapshot, StudioStatus, dashboard_snapshot, studio_status

logger = logging.getLogger(__name__)

CACHE_KEY_STUDIO = "studio_context"
SPOKESMAN_PREP_TYPE = "spokesman.prep"


@dataclass(frozen=True)
class StudioContext:
    """Human-facing snapshot used to answer questions and seed the prep cache."""

    status: StudioStatus
    pending_approval_ids: list[str] = field(default_factory=list)
    open_decision_ids: list[str] = field(default_factory=list)
    by_status: dict[str, int] = field(default_factory=dict)
    by_agent_type: dict[str, int] = field(default_factory=dict)
    recent_event_types: dict[str, int] = field(default_factory=dict)
    open_trajectories: int = 0
    closed_trajectories: int = 0
    notes: list[str] = field(default_factory=list)
    refreshed_at: str = ""

    def render_brief(self) -> str:
        """Compact text for chat / SMS answers."""
        lines = [self.status.render()]
        if self.pending_approval_ids:
            ids = ", ".join(a[:8] for a in self.pending_approval_ids[:8])
            lines.append(f"Pending approvals: {ids}")
        if self.open_decision_ids:
            ids = ", ".join(d[:8] for d in self.open_decision_ids[:8])
            lines.append(f"Open decisions: {ids}")
        if self.by_agent_type:
            top = ", ".join(
                f"{k}={v}" for k, v in list(self.by_agent_type.items())[:6]
            )
            lines.append(f"By agent type: {top}")
        for note in self.notes[:5]:
            lines.append(note)
        return "\n".join(lines)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": asdict(self.status),
            "pending_approval_ids": list(self.pending_approval_ids),
            "open_decision_ids": list(self.open_decision_ids),
            "by_status": dict(self.by_status),
            "by_agent_type": dict(self.by_agent_type),
            "recent_event_types": dict(self.recent_event_types),
            "open_trajectories": self.open_trajectories,
            "closed_trajectories": self.closed_trajectories,
            "notes": list(self.notes),
            "refreshed_at": self.refreshed_at,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StudioContext:
        st = payload.get("status") or {}
        status = StudioStatus(
            queued=int(st.get("queued") or 0),
            in_progress=int(st.get("in_progress") or 0),
            blocked=int(st.get("blocked") or 0),
            done=int(st.get("done") or 0),
            failed=int(st.get("failed") or 0),
            pending_approvals=int(st.get("pending_approvals") or 0),
            spent_tokens=int(st.get("spent_tokens") or 0),
        )
        return cls(
            status=status,
            pending_approval_ids=list(payload.get("pending_approval_ids") or []),
            open_decision_ids=list(payload.get("open_decision_ids") or []),
            by_status=dict(payload.get("by_status") or {}),
            by_agent_type=dict(payload.get("by_agent_type") or {}),
            recent_event_types=dict(payload.get("recent_event_types") or {}),
            open_trajectories=int(payload.get("open_trajectories") or 0),
            closed_trajectories=int(payload.get("closed_trajectories") or 0),
            notes=list(payload.get("notes") or []),
            refreshed_at=str(payload.get("refreshed_at") or ""),
        )


def build_studio_context(conn: psycopg.Connection) -> StudioContext:
    """Live read of studio state for answers + anticipatory cache refresh."""
    status = studio_status(conn)
    snap: DashboardSnapshot = dashboard_snapshot(conn)
    try:
        decisions = open_decisions(conn)
        open_ids = [str(d.id) for d in decisions]
    except Exception:  # noqa: BLE001 - decisions table may be absent mid-migrate
        logger.warning("open_decisions failed; continuing without")
        open_ids = []
    return StudioContext(
        status=status,
        pending_approval_ids=list(snap.pending_approval_ids),
        open_decision_ids=open_ids,
        by_status=dict(snap.by_status),
        by_agent_type=dict(snap.by_agent_type),
        recent_event_types=dict(snap.recent_event_types),
        open_trajectories=snap.open_trajectories,
        closed_trajectories=snap.closed_trajectories,
        refreshed_at=datetime.now(timezone.utc).isoformat(),
    )


def save_prep_cache(
    conn: psycopg.Connection,
    payload: dict[str, Any],
    *,
    key: str = CACHE_KEY_STUDIO,
) -> None:
    """Upsert anticipatory context into ``spokesman_prep_cache``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spokesman_prep_cache (cache_key, payload, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (cache_key) DO UPDATE
              SET payload = EXCLUDED.payload, updated_at = now()
            """,
            (key, Json(payload)),
        )
    if not conn.autocommit:
        conn.commit()


def load_prep_cache(
    conn: psycopg.Connection,
    *,
    key: str = CACHE_KEY_STUDIO,
) -> Optional[StudioContext]:
    """Return cached context, or None if missing / table absent."""
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT payload FROM spokesman_prep_cache WHERE cache_key = %s",
                (key,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        logger.warning("load_prep_cache failed")
        return None
    if not row:
        return None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    return StudioContext.from_payload(payload)


def refresh_prep_cache(
    conn: psycopg.Connection,
    *,
    notes: Optional[list[str]] = None,
) -> StudioContext:
    """Rebuild context from live DB and write the prep cache."""
    ctx = build_studio_context(conn)
    if notes:
        ctx = StudioContext(
            status=ctx.status,
            pending_approval_ids=ctx.pending_approval_ids,
            open_decision_ids=ctx.open_decision_ids,
            by_status=ctx.by_status,
            by_agent_type=ctx.by_agent_type,
            recent_event_types=ctx.recent_event_types,
            open_trajectories=ctx.open_trajectories,
            closed_trajectories=ctx.closed_trajectories,
            notes=list(notes),
            refreshed_at=ctx.refreshed_at,
        )
    try:
        save_prep_cache(conn, ctx.to_payload())
    except Exception:  # noqa: BLE001 - table may not exist yet
        logger.warning("save_prep_cache failed; context still returned live")
    return ctx


def context_for_answer(conn: psycopg.Connection) -> StudioContext:
    """Prefer fresh live context; fall back to cache if live read fails."""
    try:
        return refresh_prep_cache(conn)
    except Exception:  # noqa: BLE001
        logger.warning("live context failed; trying prep cache")
        cached = load_prep_cache(conn)
        if cached is not None:
            return cached
        raise


def emit_body_free(
    sink: Any,
    *,
    type: str,
    workstream: str = "productivity",
    payload: Optional[dict[str, Any]] = None,
    task_id: Any = None,
) -> None:
    """Emit a body-free event via an EventSink (Db / Memory / Null)."""
    sink.emit(
        make_event(
            type=type,
            workstream=workstream,
            task_id=task_id,
            payload=payload or {},
        )
    )
