"""The remote task gateway's HTTP surface (ADR-0028).

Task verbs are routed through :mod:`runtime.tasks`, so the canonical lifecycle
guard (:func:`runtime.tasks.transition`) stays the only way task state ever
changes and **no raw-SQL path exists through this service**:

| Verb | Endpoint | Scope |
| --- | --- | --- |
| list | ``GET /v1/tasks/ready`` · ``/waiting`` · ``/review`` · ``GET /v1/tasks/{id}`` | ``read`` |
| enqueue | ``POST /v1/tasks`` | ``enqueue`` |
| claim | ``POST /v1/tasks/claim`` | ``claim`` |
| heartbeat | ``POST /v1/tasks/{id}/heartbeat`` | ``claim`` |
| complete | ``POST /v1/tasks/{id}/complete`` | ``complete`` |
| agents | ``GET /v1/agents/status`` · ``/v1/studio/status`` · ``/v1/events/recent`` · ``/v1/agents/env`` | ``read`` |

``GET /health`` (liveness, no DB, no secrets) and ``GET /v1/whoami`` (any valid
token; reports the caller's own identity/scopes) round it out. New read-only
observability endpoints reuse the existing ``read`` scope so already-minted
tokens need no re-issue. Remotes may act as **any** role (PM included).

The gates live in :mod:`gateway.auth`; this module adds the request-shaped ones:
bounded bodies, bounded payloads, clamped priority/budget/limit, **claim
ownership** (a remote may only heartbeat/complete a task it holds — the runtime's
``complete_task`` has no owner check of its own), and DB-outage handling that
answers a generic ``503`` so driver text / DSNs can never leak (ADR-0017).

Observability (invariant 6): every allowed request appends a body-free
``gateway.access`` event and every *authenticated* denial a ``gateway.denied``
one — identity, verb, scope, decision code, status only. Unauthenticated denials
are logged to stderr but deliberately touch **no** database, so an
unauthenticated flood cannot make the host open connections.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from runtime.event_types import EVENT_GATEWAY_ACCESS, EVENT_GATEWAY_DENIED
from runtime.models import Assignee, TaskStatus, make_event

from .auth import (
    SCOPE_ANY,
    SCOPE_CLAIM,
    SCOPE_COMPLETE,
    SCOPE_ENQUEUE,
    SCOPE_READ,
    Allowed,
    Denied,
    RateLimiter,
    REASON_NOT_OWNER,
    authorize,
    is_identifier,
)
from .config import Settings, get_settings

logger = logging.getLogger("gateway.app")

#: Events about the remote surface itself are not workstream-scoped (they concern
#: an *identity*), so they log under the gateway's own name — the events table
#: requires a non-empty workstream (mirrors ``runtime.trust.COMMS_WORKSTREAM``).
GATEWAY_WORKSTREAM = "gateway"

#: What a remote's claimed work is tagged as in lifecycle telemetry when the
#: caller does not name a more specific role.
DEFAULT_AGENT_TYPE = "remote"

#: Generic outage reply — never the driver message, never a DSN (ADR-0017).
DB_UNAVAILABLE_DETAIL = "runtime store unavailable"

#: Cap on prerequisite edges one enqueue may declare (bounded input).
MAX_DEPENDS_ON = 32

ConnectFn = Callable[[], Any]


def _default_connect() -> Any:
    from runtime import db

    return db.connect()


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


# --- Request models ---------------------------------------------------------


def _as_identifier(v: str) -> str:
    v = v.strip().lower()
    if not is_identifier(v):
        raise ValueError(
            "must match [a-z0-9][a-z0-9._-]{0,63} (identifier-shaped, not free text)"
        )
    return v


class EnqueueRequest(BaseModel):
    """Create one task. ``workstream``/``type`` are identifier-shaped, not free text.

    ``workstream`` may be omitted when the token is pinned to exactly one — the
    same resolution the read and claim verbs use, so a pinned remote never has to
    restate what its credential already fixes.
    """

    workstream: Optional[str] = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    assignee: Optional[str] = None
    budget_tokens: Optional[int] = None
    depends_on: list[UUID] = Field(default_factory=list, max_length=MAX_DEPENDS_ON)

    @field_validator("type")
    @classmethod
    def _identifier(cls, v: str) -> str:
        return _as_identifier(v)

    @field_validator("workstream")
    @classmethod
    def _identifier_or_none(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        return _as_identifier(v)

    @field_validator("assignee")
    @classmethod
    def _known_assignee(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        value = v.strip().lower()
        if value not in {a.value for a in Assignee}:
            raise ValueError(f"assignee must be one of {[a.value for a in Assignee]}")
        return value


class ClaimRequest(BaseModel):
    """Grab + start the next grabbable task for this identity.

    Remotes may act as **any** role (PM, executor, …) via ``agent_type`` — that
    is a bookkeeping label, not a pool filter. ``assignee`` defaults to
    ``offhost`` (also matches unassigned) so a remote never steals work
    explicitly pinned to ``host``; pass ``assignee=host`` only when deliberately
    taking host-pool work.
    """

    workstream: Optional[str] = None
    agent_type: Optional[str] = None
    #: Which pool to grab from; ``offhost`` (the default) also matches unassigned
    #: tasks, so a remote never steals work explicitly pinned to the host.
    assignee: Optional[str] = Assignee.OFFHOST.value

    @field_validator("workstream", "agent_type")
    @classmethod
    def _identifier_or_none(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not str(v).strip():
            return None
        value = str(v).strip().lower()
        if not is_identifier(value):
            raise ValueError("must match [a-z0-9][a-z0-9._-]{0,63}")
        return value

    @field_validator("assignee")
    @classmethod
    def _offhost_only(cls, v: Optional[str]) -> str:
        """A remote may ONLY ever grab from the ``offhost`` pool (or unassigned).

        The pool the queue grabs from is ``(assignee IS NULL OR assignee = value)``
        (``runtime.tasks.grab_task``) — and a ``None`` assignee drops that clause
        entirely, so the grab spans EVERY pool including ``host``. That makes both
        ``host`` *and* a blank/``null`` assignee steal host-pinned work, exactly
        what ADR-0028 forbids ("Cannot: claim work pinned to host").

        So this validator NEVER yields ``None``: an omitted key uses the field
        default (``offhost``), and an explicit ``null``/blank (which still runs the
        validator, bypassing the default) is coerced to ``offhost`` too. Anything
        other than ``offhost`` — ``host`` or any unknown value — is rejected at
        validation with a 422, so an out-of-range string can never reach
        ``Assignee(...)`` and blow up as an ungraceful 500. The model therefore
        always carries ``offhost``, and the handler never passes ``None`` on.
        """
        if v is None or not str(v).strip():
            return Assignee.OFFHOST.value
        value = str(v).strip().lower()
        if value != Assignee.OFFHOST.value:
            raise ValueError(
                f"assignee must be {Assignee.OFFHOST.value!r} "
                "(a remote may not grab from the host pool)"
            )
        return value


class CompleteRequest(BaseModel):
    """Finalize a task this identity holds."""

    status: str = TaskStatus.MERGED.value
    result: Optional[dict[str, Any]] = None
    spent_tokens: Optional[int] = Field(default=None, ge=0)

    @field_validator("status")
    @classmethod
    def _terminal_only(cls, v: str) -> str:
        value = (v or "").strip().lower()
        if value not in {TaskStatus.MERGED.value, TaskStatus.ABANDONED.value}:
            raise ValueError("status must be 'merged' or 'abandoned'")
        return value


# --- App --------------------------------------------------------------------


def create_app(
    settings: Optional[Settings] = None,
    *,
    connect: Optional[ConnectFn] = None,
    limiter: Optional[RateLimiter] = None,
) -> FastAPI:
    """Application factory (tests inject settings / a connect factory / a clock).

    ``connect`` returns an open runtime DB connection (defaults to the live
    ``runtime.db.connect``); every connection this app opens, it closes.
    """
    settings = settings or get_settings()
    connect = connect or _default_connect
    limiter = limiter if limiter is not None else settings.new_limiter()

    app = FastAPI(title="AI Studio Task Gateway", version="1.0.0")
    app.state.settings = settings

    # --- Bounded bodies (a remote cannot make the host buffer arbitrary bytes) --
    @app.middleware("http")
    async def _limit_body(request: Request, call_next):  # noqa: ANN001
        if request.method in {"POST", "PUT", "PATCH"}:
            if "chunked" in request.headers.get("transfer-encoding", "").lower():
                # An undeclared (streamed) length is refused rather than buffered
                # without bound; every client this gateway supports sends a length.
                return JSONResponse(
                    {"detail": "content-length required"}, status_code=411
                )
            raw_len = request.headers.get("content-length")
            if raw_len is not None:
                try:
                    declared = int(raw_len)
                except ValueError:
                    return JSONResponse(
                        {"detail": "invalid content-length"}, status_code=400
                    )
                if declared > settings.max_body_bytes:
                    return JSONResponse(
                        {"detail": f"body exceeds {settings.max_body_bytes} bytes"},
                        status_code=413,
                    )
        return await call_next(request)

    # --- Audit (body-free; never a token, payload or driver string) ------------

    def _audit(
        conn: Any,
        *,
        type: str,
        identity: Optional[str],
        verb: str,
        scope: str,
        status: int,
        reason: Optional[str] = None,
        workstream: Optional[str] = None,
        task_id: Optional[UUID] = None,
    ) -> None:
        from runtime.events import append_event

        try:
            append_event(
                conn,
                make_event(
                    workstream=workstream or GATEWAY_WORKSTREAM,
                    type=type,
                    task_id=task_id,
                    payload={
                        "identity": identity,
                        "verb": verb,
                        "scope": scope,
                        "status": status,
                        "reason": reason,
                    },
                ),
            )
        except Exception:  # noqa: BLE001 - telemetry must never fail a request
            logger.warning("gateway audit append failed (verb=%s)", verb)

    def _audit_denied(denied: Denied, *, verb: str, scope: str) -> None:
        """Record an AUTHENTICATED denial. Unauthenticated ones touch no DB.

        Deliberate: opening a connection for an unauthenticated request would let
        an internet-facing flood exhaust the host's DB connections. Those are
        logged to stderr only.
        """
        if denied.identity is None:
            logger.warning(
                "gateway denied verb=%s scope=%s status=%s reason=%s (unauthenticated)",
                verb, scope, denied.status, denied.reason,
            )
            return
        logger.warning(
            "gateway denied identity=%s verb=%s scope=%s status=%s reason=%s",
            denied.identity, verb, scope, denied.status, denied.reason,
        )
        try:
            conn = connect()
        except Exception:  # noqa: BLE001 - degraded store: the stderr line stands
            return
        try:
            _audit(
                conn, type=EVENT_GATEWAY_DENIED, identity=denied.identity, verb=verb,
                scope=scope, status=denied.status, reason=denied.reason,
            )
        finally:
            _close(conn)

    # --- The gate -------------------------------------------------------------

    def _gate(
        *,
        authorization: Optional[str],
        scope: str,
        verb: str,
        workstream: Optional[str] = None,
        workstream_optional: bool = False,
    ) -> Allowed:
        """Authorize one request or raise the mapped ``HTTPException``.

        ``workstream_optional=True`` is for the endpoints not scoped by a
        caller-supplied workstream (whoami / read-one-task / studio-status /
        agents-env): a token pinned to several workstreams must not be locked out
        of them for lacking a single default (see :func:`gateway.auth.authorize`).
        """
        decision = authorize(
            settings.tokens, limiter,
            authorization=authorization, scope=scope, workstream=workstream,
            workstream_optional=workstream_optional,
        )
        if isinstance(decision, Denied):
            _audit_denied(decision, verb=verb, scope=scope)
            headers = None
            if decision.retry_after is not None:
                headers = {"Retry-After": str(int(decision.retry_after))}
            raise HTTPException(
                status_code=decision.status,
                detail=decision.reason,
                headers=headers,
            )
        return decision

    def _open(verb: str) -> Any:
        """Open a DB connection or fail with a generic 503 (never driver text)."""
        try:
            return connect()
        except Exception:  # noqa: BLE001 - outage ⇒ degrade cleanly (ADR-0017)
            logger.warning("gateway %s: runtime store unreachable", verb)
            raise HTTPException(status_code=503, detail=DB_UNAVAILABLE_DETAIL)

    def _clamp_limit(limit: Optional[int]) -> int:
        if limit is None or limit <= 0:
            return settings.max_limit
        return min(int(limit), settings.max_limit)

    def _task_json(task: Any) -> dict:
        return json.loads(task.model_dump_json())

    def _require_visible(task: Any, allowed: Allowed, *, verb: str) -> None:
        """A pinned token may only touch tasks in its own workstream(s) (ADR-0018)."""
        if not allowed.token.allows_workstream(task.workstream):
            _audit_denied(
                Denied(403, "workstream_denied", identity=allowed.identity),
                verb=verb, scope=SCOPE_READ,
            )
            raise HTTPException(status_code=403, detail="workstream_denied")

    # --- Public ---------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        """Liveness only: no DB touch, no secrets — safe to expose on the tunnel."""
        return {
            "status": "ok",
            "service": "task-gateway",
            "tokens_configured": len(settings.tokens),
        }

    @app.get("/v1/whoami")
    def whoami(authorization: Optional[str] = Header(default=None)) -> dict:
        """Echo the caller's own identity/scopes — the remote's first smoke test."""
        allowed = _gate(
            authorization=authorization, scope=SCOPE_ANY, verb="whoami",
            workstream_optional=True,
        )
        return {
            "identity": allowed.identity,
            "scopes": sorted(allowed.token.scopes),
            "workstreams": sorted(allowed.token.workstreams),
            "default_workstream": allowed.workstream,
        }

    # --- Read -----------------------------------------------------------------

    def _list(
        verb: str,
        authorization: Optional[str],
        workstream: Optional[str],
        limit: Optional[int],
    ) -> tuple[Allowed, Any, int]:
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb=verb,
            workstream=workstream,
        )
        return allowed, _open(verb), _clamp_limit(limit)

    @app.get("/v1/tasks/ready")
    def list_ready(
        workstream: Optional[str] = None,
        limit: Optional[int] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Tasks grabbable NOW (``up_for_grabs``, every prerequisite merged)."""
        from runtime.tasks import ready_tasks

        allowed, conn, capped = _list("list_ready", authorization, workstream, limit)
        try:
            tasks = ready_tasks(conn, workstream=allowed.workstream, limit=capped)
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="list_ready", scope=SCOPE_READ, status=200,
                workstream=allowed.workstream,
            )
            return {"tasks": [_task_json(t) for t in tasks], "count": len(tasks)}
        finally:
            _close(conn)

    @app.get("/v1/tasks/waiting")
    def list_waiting(
        workstream: Optional[str] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """``up_for_grabs`` tasks blocked by an unmet/abandoned prerequisite."""
        from runtime.tasks import waiting_tasks

        allowed, conn, _ = _list("list_waiting", authorization, workstream, None)
        try:
            rows = waiting_tasks(conn, workstream=allowed.workstream)
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="list_waiting", scope=SCOPE_READ, status=200,
                workstream=allowed.workstream,
            )
            return {
                "tasks": [
                    {
                        "task": _task_json(row["task"]),
                        "pending_prereqs": [str(p) for p in row["pending_prereqs"]],
                        "blocked_by_abandoned": row["blocked_by_abandoned"],
                    }
                    for row in rows
                ],
                "count": len(rows),
            }
        finally:
            _close(conn)

    @app.get("/v1/tasks/review")
    def list_review(
        workstream: Optional[str] = None,
        limit: Optional[int] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Tasks awaiting review (``ready_for_review``), oldest first."""
        from runtime.tasks import list_for_review

        allowed, conn, capped = _list("list_review", authorization, workstream, limit)
        try:
            tasks = list_for_review(conn, workstream=allowed.workstream, limit=capped)
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="list_review", scope=SCOPE_READ, status=200,
                workstream=allowed.workstream,
            )
            return {"tasks": [_task_json(t) for t in tasks], "count": len(tasks)}
        finally:
            _close(conn)

    @app.get("/v1/tasks/{task_id}")
    def read_task(
        task_id: UUID,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """One task row by id (subject to the token's workstream pinning)."""
        from runtime.tasks import get_task

        # Gate loosely (any valid token with workstream access passes), then let
        # _require_visible enforce the token's pin against THIS task's workstream —
        # a multi-pinned token has no single default to gate on up front.
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb="read_task",
            workstream_optional=True,
        )
        conn = _open("read_task")
        try:
            task = get_task(conn, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            _require_visible(task, allowed, verb="read_task")
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="read_task", scope=SCOPE_READ, status=200,
                workstream=task.workstream, task_id=task.id,
            )
            return {"task": _task_json(task)}
        finally:
            _close(conn)

    # --- Enqueue --------------------------------------------------------------

    @app.post("/v1/tasks", status_code=201)
    def enqueue(
        req: EnqueueRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Create an ``up_for_grabs`` task (priority/budget/payload all bounded)."""
        from runtime.tasks import enqueue_task

        allowed = _gate(
            authorization=authorization, scope=SCOPE_ENQUEUE, verb="enqueue",
            workstream=req.workstream,
        )
        if allowed.workstream is None:
            # Unpinned token (or pinned to several): nothing to infer from, and
            # guessing a workstream is exactly the widening pinning prevents.
            raise HTTPException(status_code=422, detail="workstream_required")
        payload_bytes = len(json.dumps(req.payload).encode("utf-8"))
        if payload_bytes > settings.max_payload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"payload exceeds {settings.max_payload_bytes} bytes",
            )
        # A remote may not outrank host work, nor mint an unbounded budget.
        priority = max(-settings.max_priority, min(settings.max_priority, req.priority))
        budget_tokens = req.budget_tokens
        if budget_tokens is not None:
            budget_tokens = max(0, min(int(budget_tokens), settings.max_budget_tokens))

        conn = _open("enqueue")
        try:
            task = enqueue_task(
                conn,
                workstream=allowed.workstream,
                type=req.type,
                payload={**req.payload, "enqueued_by": allowed.identity},
                priority=priority,
                assignee=Assignee(req.assignee) if req.assignee else None,
                budget_tokens=budget_tokens,
                depends_on=list(req.depends_on) or None,
            )
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="enqueue", scope=SCOPE_ENQUEUE, status=201,
                workstream=task.workstream, task_id=task.id,
            )
            return {"task": _task_json(task)}
        except HTTPException:
            raise
        except ValueError as exc:  # dependency cycle / rejected input
            raise HTTPException(status_code=422, detail=str(exc))
        finally:
            _close(conn)

    # --- Claim / heartbeat / complete ----------------------------------------

    @app.post("/v1/tasks/claim")
    def claim(
        body: Optional[ClaimRequest] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Grab the next grabbable task and start it, as THIS identity.

        ``claimed_by``/``agent_id`` is the token identity, so remote work is
        attributable and the supervisor treats it like any other worker (a stale
        heartbeat is re-kicked). Returns ``{"task": null}`` when nothing is
        grabbable — an empty queue is not an error.
        """
        from runtime.tasks import claim_task

        req = body or ClaimRequest()
        allowed = _gate(
            authorization=authorization, scope=SCOPE_CLAIM, verb="claim",
            workstream=req.workstream,
        )
        conn = _open("claim")
        try:
            # ``req.assignee`` is validated to be ``offhost`` and never blank/None
            # (see ClaimRequest._offhost_only), so a remote is ALWAYS constrained to
            # the offhost pool — passing ``assignee=None`` here would drop the pool
            # clause and grab host-pinned work (ADR-0028).
            task = claim_task(
                conn,
                worker_id=allowed.identity,
                assignee=Assignee(req.assignee),
                workstream=allowed.workstream,
                # Bookkeeping label only — does not filter which types are grabbable.
                # Remotes pass agent_type=pm (etc.) when acting as that role.
                agent_type=req.agent_type or DEFAULT_AGENT_TYPE,
            )
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="claim", scope=SCOPE_CLAIM, status=200,
                workstream=(task.workstream if task else allowed.workstream),
                task_id=(task.id if task else None),
            )
            return {"task": _task_json(task) if task is not None else None}
        finally:
            _close(conn)

    @app.get("/v1/agents/status")
    def agents_status(
        workstream: Optional[str] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Who is running what: in-flight tasks + heartbeats (``read`` scope).

        Ids / types / identities / timestamps only — no payloads or secrets.
        Uses existing ``read`` so currently minted tokens need no re-issue.
        """
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb="agents_status",
            workstream=workstream,
        )
        conn = _open("agents_status")
        try:
            ws = allowed.workstream
            clauses = [
                "status IN ('claimed','in_progress','ready_for_review',"
                "'reviewer_blocked','approved','blocked')"
            ]
            params: list[object] = []
            if ws is not None:
                clauses.append("workstream = %s")
                params.append(ws)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, workstream, type, status, agent_type, claimed_by, "
                    "assignee, priority, heartbeat_at, claimed_at, updated_at "
                    f"FROM tasks WHERE {' AND '.join(clauses)} "
                    "ORDER BY heartbeat_at DESC NULLS LAST, updated_at DESC "
                    "LIMIT 200",
                    params,
                )
                rows = cur.fetchall()
            agents = [
                {
                    "task_id": str(r["id"]),
                    "workstream": r["workstream"],
                    "type": r["type"],
                    "status": r["status"],
                    "agent_type": r["agent_type"],
                    "identity": r["claimed_by"],
                    "assignee": r["assignee"],
                    "priority": r["priority"],
                    "heartbeat_at": (
                        r["heartbeat_at"].isoformat() if r["heartbeat_at"] else None
                    ),
                    "claimed_at": (
                        r["claimed_at"].isoformat() if r["claimed_at"] else None
                    ),
                    "updated_at": (
                        r["updated_at"].isoformat() if r["updated_at"] else None
                    ),
                }
                for r in rows
            ]
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="agents_status", scope=SCOPE_READ, status=200,
                workstream=ws,
            )
            return {"agents": agents, "count": len(agents)}
        finally:
            _close(conn)

    @app.get("/v1/studio/status")
    def studio_status_view(
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Aggregate studio queue pulse (``read`` scope) — test traffic filtered.

        Scope is the token's FULL workstream pin-set, not ``allowed.workstream``:
        a multi-pinned token has ``default_workstream() == None``, and passing that
        as ``workstream`` would make ``studio_status`` aggregate over EVERY
        workstream — leaking counts/spend/approvals from verticals the token is not
        pinned to (ADR-0018/0028). Unpinned ⇒ empty pin-set ⇒ full studio view.
        """
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb="studio_status",
            workstream_optional=True,
        )
        conn = _open("studio_status")
        try:
            from spokesman.runtime_bridge import studio_status as _studio_status

            pins = sorted(allowed.token.workstreams)  # [] when unpinned = full view
            snap = _studio_status(conn, workstreams=pins or None)
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="studio_status", scope=SCOPE_READ, status=200,
                workstream=allowed.workstream,
            )
            return {
                "queued": snap.queued,
                "in_progress": snap.in_progress,
                "blocked": snap.blocked,
                "done": snap.done,
                "failed": snap.failed,
                "pending_approvals": snap.pending_approvals,
                "spent_tokens": snap.spent_tokens,
                "open_tasks": snap.open_tasks,
                "workstream": allowed.workstream,
                "workstreams": pins,
            }
        finally:
            _close(conn)

    @app.get("/v1/events/recent")
    def events_recent(
        limit: Optional[int] = None,
        workstream: Optional[str] = None,
        task_id: Optional[str] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Recent event-log pulse (``read`` scope) — types/ids only, no bodies."""
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb="events_recent",
            workstream=workstream,
        )
        conn = _open("events_recent")
        try:
            capped = max(1, min(int(limit or 50), 200))
            ws = allowed.workstream
            clauses = ["1=1"]
            params: list[object] = []
            if ws is not None:
                clauses.append("workstream = %s")
                params.append(ws)
            if task_id:
                try:
                    tid = UUID(str(task_id).strip())
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail="task_id must be a UUID",
                    ) from exc
                clauses.append("task_id = %s")
                params.append(tid)
            params.append(capped)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, type, workstream, task_id, ts "
                    f"FROM events WHERE {' AND '.join(clauses)} "
                    "ORDER BY seq DESC NULLS LAST LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            events = [
                {
                    "seq": r["seq"],
                    "type": r["type"],
                    "workstream": r["workstream"],
                    "task_id": str(r["task_id"]) if r["task_id"] else None,
                    "ts": r["ts"].isoformat() if r["ts"] else None,
                }
                for r in rows
            ]
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="events_recent", scope=SCOPE_READ, status=200,
                workstream=ws,
            )
            return {"events": events, "count": len(events)}
        finally:
            _close(conn)

    @app.get("/v1/agents/env")
    def agents_env(
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Non-secret host markers a remote can use for orientation (``read``).

        Never returns DSNs, tokens, API keys, or personal data (ADR-0011).
        """
        allowed = _gate(
            authorization=authorization, scope=SCOPE_READ, verb="agents_env",
            workstream_optional=True,
        )
        from runtime.traffic import default_traffic

        payload = {
            "traffic_default": default_traffic(),
            "identity": allowed.identity,
            "scopes": sorted(allowed.token.scopes),
            "workstream_pin": allowed.workstream,
            "gateway_workstream": GATEWAY_WORKSTREAM,
        }
        # Audit without opening a DB when possible — still record when DB is up.
        conn = None
        try:
            conn = _open("agents_env")
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="agents_env", scope=SCOPE_READ, status=200,
            )
        except Exception:  # noqa: BLE001 - env view must not fail on audit/store outage
            logger.warning("agents_env audit skipped (store unavailable)")
        finally:
            if conn is not None:
                _close(conn)
        return payload

    def _held_task(conn: Any, task_id: UUID, allowed: Allowed, *, verb: str) -> Any:
        """Fetch a task this identity is allowed to act on, or raise 404/403.

        The **claim-ownership gate**: a remote may only heartbeat/complete a task
        whose ``claimed_by`` is its own identity, so a valid token cannot finish
        (or falsely claim credit for) another worker's task.
        """
        from runtime.tasks import get_task

        task = get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        _require_visible(task, allowed, verb=verb)
        if task.claimed_by != allowed.identity:
            _audit_denied(
                Denied(403, REASON_NOT_OWNER, identity=allowed.identity),
                verb=verb, scope=SCOPE_CLAIM,
            )
            raise HTTPException(status_code=403, detail=REASON_NOT_OWNER)
        return task

    @app.post("/v1/tasks/{task_id}/heartbeat")
    def heartbeat_task(
        task_id: UUID,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Refresh the heartbeat of a task this identity holds (liveness)."""
        from runtime.tasks import heartbeat

        allowed = _gate(
            authorization=authorization, scope=SCOPE_CLAIM, verb="heartbeat",
        )
        conn = _open("heartbeat")
        try:
            held = _held_task(conn, task_id, allowed, verb="heartbeat")
            beat = heartbeat(conn, task_id, allowed.identity)
            if beat is None:
                raise HTTPException(
                    status_code=409, detail="task no longer held (state changed)"
                )
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="heartbeat", scope=SCOPE_CLAIM, status=200,
                workstream=held.workstream, task_id=task_id,
            )
            return {"task": _task_json(beat)}
        finally:
            _close(conn)

    @app.post("/v1/tasks/{task_id}/complete")
    def complete(
        task_id: UUID,
        body: Optional[CompleteRequest] = None,
        authorization: Optional[str] = Header(default=None),
    ) -> dict:
        """Finalize a task this identity holds — ``merged`` or ``abandoned``.

        Runs through ``runtime.tasks.complete_task`` (never a status write), so a
        success still travels the canonical ``in_progress → ready_for_review →
        approved → merged`` path with its full lifecycle telemetry.
        """
        from runtime.tasks import complete_task

        req = body or CompleteRequest()
        allowed = _gate(
            authorization=authorization, scope=SCOPE_COMPLETE, verb="complete",
        )
        conn = _open("complete")
        try:
            held = _held_task(conn, task_id, allowed, verb="complete")
            result = dict(req.result or {})
            result["completed_by"] = allowed.identity
            finished = complete_task(
                conn, task_id,
                result=result,
                status=TaskStatus(req.status),
                spent_tokens=req.spent_tokens,
            )
            if finished is None:
                raise HTTPException(
                    status_code=409,
                    detail="task not in_progress (already finalized or re-kicked)",
                )
            _audit(
                conn, type=EVENT_GATEWAY_ACCESS, identity=allowed.identity,
                verb="complete", scope=SCOPE_COMPLETE, status=200,
                workstream=held.workstream, task_id=task_id,
            )
            return {"task": _task_json(finished)}
        finally:
            _close(conn)

    return app


#: The ASGI entry point (``uvicorn gateway.app:app``). A malformed
#: ``TASK_GATEWAY_TOKENS`` fails HERE, at startup, loudly.
app = create_app()
