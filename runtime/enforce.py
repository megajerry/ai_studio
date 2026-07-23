"""Enforced invocation path — the ONLY way an agent runs a tool.

CLAUDE.md invariants 2, 3 & 6: agents never touch the host, tools are
permissioned, everything emits events. :func:`invoke` is the choke point that
makes those true together:

1. Resolve the tool from the registry and the capabilities the call needs.
2. Ask the policy engine (:mod:`runtime.policy`) for a :class:`Decision`.
3. Emit ``policy.decision`` — always, before acting.
4. Branch on the decision:
   - **ALLOW** → emit ``tool.invoked`` and run the tool (🟡 is logged by these
     very events).
   - **NEEDS_APPROVAL** (🔴 / over-budget) → with a ``conn``, first look for a
     live one-shot **grant** (:mod:`runtime.approvals`): a human-approved grant
     turns this into an ALLOW (execute once, then consume the grant); otherwise
     persist a ``pending`` approval and return PENDING **without executing**.
     With no ``conn`` it pends ephemerally. This path NEVER auto-approves — only
     an explicit human-resolved grant lets a 🔴 action run (ADR-0006).
   - **DENY** → return a DENIED result without executing.

**Agents must only ever call `invoke`, never a tool's `execute` directly.** The
tool objects live behind the registry and the policy gate; bypassing `invoke`
would bypass least privilege, the 🔴 approval gate, and the event log.

Events go through an injected :class:`EventSink` so the logic is testable with no
database: production passes :class:`DbEventSink` (writes to the M1 append-only
log); tests pass :class:`MemoryEventSink`; :class:`NullEventSink` drops events.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .approvals import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_APPROVAL_RESOLVED,
    compute_fingerprint,
    consume_grant,
    find_grant,
    request_approval,
)
from .capabilities import ActionTier
from .event_types import EVENT_POLICY_DECISION, EVENT_TOOL_INVOKED
from .models import EventIn, make_event
from .policy import Decision, Effect, PolicyConfig, PolicyRequest, decide, load_policy
from .tools import ToolRegistry
from .tools.base import ToolResult

# The event types this layer emits (``policy.decision`` / ``tool.invoked``) come
# from the canonical :mod:`runtime.event_types`; the events table's ``type``
# column is free-form text (see runtime/README.md). The approval.* wires are
# imported from :mod:`runtime.approvals` above and re-exported here for a single
# import surface.


# --- Event sinks ------------------------------------------------------------


class EventSink(Protocol):
    """Anything that can record an event. Decouples enforcement from the DB."""

    def emit(self, event: EventIn) -> None:
        ...


class NullEventSink:
    """Drops events (only for callers that explicitly opt out of logging)."""

    def emit(self, event: EventIn) -> None:  # noqa: D401 - trivial
        return None


class MemoryEventSink:
    """Collects events in memory — for tests and dry runs."""

    def __init__(self) -> None:
        self.events: list[EventIn] = []

    def emit(self, event: EventIn) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class DbEventSink:
    """Persists events to the M1 append-only log via ``runtime.events.append_event``."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def emit(self, event: EventIn) -> None:
        # Imported lazily so policy/enforce logic never hard-depends on psycopg.
        from .events import append_event

        append_event(self._conn, event)


# --- Result type ------------------------------------------------------------


class InvokeStatus(str, Enum):
    EXECUTED = "executed"
    DENIED = "denied"
    PENDING = "pending"


class InvokeResult(BaseModel):
    """Outcome of an enforced invocation."""

    status: InvokeStatus
    decision: Decision
    tool: str
    result: Optional[ToolResult] = None
    #: Set only when status is PENDING — correlates the approval.requested event
    #: with the eventual resolution (ADR-0006).
    approval_id: Optional[UUID] = None

    @property
    def executed(self) -> bool:
        return self.status is InvokeStatus.EXECUTED


def _arg_keys(kwargs: dict) -> list[str]:
    """Log which arguments were passed, not their values.

    Keeps file contents out of the log and guarantees no secret value is ever
    written to the event stream (secrets never arrive as kwargs anyway — tools
    read them from env, per ADR-0011 — but this is belt-and-suspenders).
    """
    return sorted(kwargs)


def invoke(
    role: str,
    tool_name: str,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig] = None,
    events: Optional[EventSink] = None,
    conn: Any = None,
    workstream: str = "productivity",
    budget=None,
    task_id: Optional[UUID] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    **kwargs: Any,
) -> InvokeResult:
    """Run ``tool_name`` on behalf of ``role`` through the policy gate.

    This is the enforced path; see the module docstring. ``registry`` supplies
    the tools, ``config`` the policy rules (loaded from the resolved policy file
    if omitted), and ``events`` the sink (defaults to :class:`NullEventSink`;
    production should pass a :class:`DbEventSink`).

    ``conn`` opts the call into the **persisted approval loop** (:mod:`runtime.approvals`):
    on a NEEDS_APPROVAL decision, an existing one-shot grant lets the call execute
    (and the grant is consumed); otherwise a durable ``pending`` approval is
    created and the call PENDs. With ``conn=None`` the call behaves as before —
    it pends ephemerally with no persistence, keeping pure unit tests DB-free.
    """
    if config is None:
        config = load_policy()
    if events is None:
        events = NullEventSink()

    tool = registry.get(tool_name)
    if tool is None:
        # Unknown tool → treat as a denial; nothing runs, and we record why.
        decision = Decision(
            effect=Effect.DENY,
            tier=ActionTier.RED,
            reason=f"unknown tool: {tool_name!r}",
            role=role,
            tool=tool_name,
        )
        _emit_decision(events, decision, workstream, task_id, trace_id, span_id, kwargs)
        return InvokeResult(status=InvokeStatus.DENIED, decision=decision, tool=tool_name)

    required = tool.capabilities_for(**kwargs)
    request = PolicyRequest(
        role=role,
        tool=tool_name,
        required_capabilities=required,
        budget=budget,
    )
    decision = decide(request, config)
    _emit_decision(events, decision, workstream, task_id, trace_id, span_id, kwargs)

    if decision.effect is Effect.DENY:
        return InvokeResult(status=InvokeStatus.DENIED, decision=decision, tool=tool_name)

    if decision.effect is Effect.NEEDS_APPROVAL:
        # A live grant (find_grant) turns 🔴 into a one-shot ALLOW; else PEND.
        if conn is not None:
            fingerprint = compute_fingerprint(
                task_id, tool_name, sorted(c.value for c in required)
            )
            grant = find_grant(conn, fingerprint)
            if grant is not None and consume_grant(conn, grant.id) is not None:
                # One grant = one execution. Execute, noting which grant authorized it.
                return _execute(
                    tool, tool_name, role, decision, events, workstream,
                    task_id, trace_id, span_id, kwargs, approval_id=grant.id,
                )
            # No grant (or lost the race for it) → persist a pending request.
            approval = request_approval(
                conn,
                task_id=task_id,
                role=role,
                tool=tool_name,
                capabilities=sorted(c.value for c in required),
                tier=decision.tier.value,
                reason=decision.reason,
                sink=events,
                workstream=workstream,
                fingerprint=fingerprint,
            )
            return InvokeResult(
                status=InvokeStatus.PENDING,
                decision=decision,
                tool=tool_name,
                approval_id=approval.id,
            )

        # No-conn path (pure/unit): pend ephemerally, no persistence (as before).
        approval_id = uuid4()
        events.emit(
            make_event(
                workstream=workstream,
                type=EVENT_APPROVAL_REQUESTED,
                task_id=task_id,
                trace_id=trace_id,
                span_id=span_id,
                payload={
                    "approval_id": str(approval_id),
                    "arg_keys": _arg_keys(kwargs),
                    **decision.to_payload(),
                },
            )
        )
        # Pending — DO NOT execute. The stakeholder loop resolves this later.
        return InvokeResult(
            status=InvokeStatus.PENDING,
            decision=decision,
            tool=tool_name,
            approval_id=approval_id,
        )

    # ALLOW (🟢 or 🟡). Record the invocation, then run the tool.
    return _execute(
        tool, tool_name, role, decision, events, workstream,
        task_id, trace_id, span_id, kwargs,
    )


def _execute(
    tool: Any,
    tool_name: str,
    role: str,
    decision: Decision,
    events: EventSink,
    workstream: str,
    task_id: Optional[UUID],
    trace_id: Optional[str],
    span_id: Optional[str],
    kwargs: dict,
    approval_id: Optional[UUID] = None,
) -> InvokeResult:
    """Emit ``tool.invoked`` then run the tool. ``approval_id`` is set only when a
    🔴 grant authorized this run (one grant = one execution)."""
    payload = {
        "tool": tool_name,
        "role": role,
        "tier": decision.tier.value,
        "arg_keys": _arg_keys(kwargs),
    }
    if approval_id is not None:
        payload["approval_id"] = str(approval_id)
    events.emit(
        make_event(
            workstream=workstream,
            type=EVENT_TOOL_INVOKED,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            payload=payload,
        )
    )
    result = tool.execute(**kwargs)
    return InvokeResult(
        status=InvokeStatus.EXECUTED,
        decision=decision,
        tool=tool_name,
        result=result,
        approval_id=approval_id,
    )


def _emit_decision(
    events: EventSink,
    decision: Decision,
    workstream: str,
    task_id: Optional[UUID],
    trace_id: Optional[str],
    span_id: Optional[str],
    kwargs: dict,
) -> None:
    events.emit(
        make_event(
            workstream=workstream,
            type=EVENT_POLICY_DECISION,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            payload={"arg_keys": _arg_keys(kwargs), **decision.to_payload()},
        )
    )
