"""Conversational inbound path for Spokesman (ADR-0026).

Keyword shortcuts stay in ``app.handle_inbound_command``. Free text lands here:
classify intent → answer from grounded context / enqueue ``pm.tick`` /
``spokesman.prep`` / propose handoff. Never calls PM directly.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional
from uuid import UUID

from runtime.enforce import DbEventSink, EventSink, NullEventSink
from runtime.event_types import (
    EVENT_HUMAN_GOAL,
    EVENT_HUMAN_MESSAGE,
    EVENT_SPOKESMAN_PREP_READY,
)
from runtime.grounding import Claim
from runtime.scheduler import PM_TICK_TYPE
from runtime.tasks import enqueue_task

from .context import (
    SPOKESMAN_PREP_TYPE,
    StudioContext,
    context_for_answer,
    emit_body_free,
    refresh_prep_cache,
)
from .handoff import format_handoff_relay, propose_handoff

logger = logging.getLogger(__name__)

ConnectFn = Callable[[], Any]


class Intent(str, Enum):
    ANSWER = "answer"
    ENQUEUE_GOAL = "enqueue_goal"
    NEED_PREP = "need_prep"
    PROPOSE_HANDOFF = "propose_handoff"


@dataclass
class ConverseOutcome:
    intent: Intent
    replies: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


_QUESTION_RE = re.compile(
    r"^(who|what|when|where|why|how|is|are|do|does|did|can|could|would|will|"
    r"should|status|any|which)\b|\?$",
    re.I,
)
_GOAL_RE = re.compile(
    r"\b(please |pls )?(build|implement|create|ship|fix|add|remove|change|"
    r"update|investigate|research|plan|start|stop|prioritize|focus on|"
    r"i want|i need|we need|work on|make sure)\b",
    re.I,
)
_HANDOFF_RE = re.compile(
    r"\b(talk (directly )?to|hand ?off|speak (with|to)|bring in)\b.*(pm|critic|"
    r"reviewer|researcher|agent)|"
    r"\b(pm|critic|reviewer)\b.*(talk|discuss|directly)\b",
    re.I,
)
_PREP_RE = re.compile(
    r"\b(dig into|look up|check (the )?(logs|db|database|budget|spend)|"
    r"investigate (why|how)|deeper (look|dive)|find out)\b",
    re.I,
)
_END_HANDOFF_RE = re.compile(r"^(end handoff|stop handoff|exit handoff)\b", re.I)


def classify_intent(text: str) -> tuple[Intent, dict[str, Any]]:
    """Heuristic classifier (also used when model dry-run returns stubs)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return Intent.ANSWER, {"topic": "empty"}
    if _HANDOFF_RE.search(cleaned):
        role = "pm"
        for candidate in ("critic", "reviewer", "researcher", "pm"):
            if re.search(rf"\b{candidate}\b", cleaned, re.I):
                role = candidate
                break
        return Intent.PROPOSE_HANDOFF, {"role": role, "reason": cleaned[:300]}
    if _PREP_RE.search(cleaned) and not _QUESTION_RE.search(cleaned.split()[0]):
        return Intent.NEED_PREP, {"questions": [cleaned]}
    if _GOAL_RE.search(cleaned) and not cleaned.endswith("?"):
        return Intent.ENQUEUE_GOAL, {"goal": cleaned}
    if _QUESTION_RE.search(cleaned) or cleaned.endswith("?"):
        return Intent.ANSWER, {"topic": cleaned}
    # Imperative without keyword → treat as goal; otherwise answer from context.
    if len(cleaned.split()) >= 4 and cleaned[0].isupper():
        return Intent.ENQUEUE_GOAL, {"goal": cleaned}
    return Intent.ANSWER, {"topic": cleaned}


def _try_model_classify(text: str, conn: Any) -> Optional[tuple[Intent, dict[str, Any]]]:
    """Optional LLM classify; returns None on dry-run / parse failure."""
    try:
        from runtime.model import call_model
    except Exception:  # noqa: BLE001
        return None
    prompt = (
        "Classify the stakeholder message for AI Studio Spokesman.\n"
        "Return ONLY JSON: "
        '{"intent":"answer|enqueue_goal|need_prep|propose_handoff",'
        '"goal":"...", "questions":["..."], "role":"pm", "reason":"..."}\n'
        f"Message: {text[:1500]}"
    )
    try:
        completion = call_model(
            "spokesman",
            "converse",
            [{"role": "user", "content": prompt}],
            quality="standard",
            conn=conn,
            sink=NullEventSink(),
            workstream="productivity",
            force_dry_run=False,
        )
        raw = (completion.text or "").strip()
        if raw.startswith("[dry-run:"):
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0:
            return None
        data = json.loads(raw[start : end + 1])
        intent_s = str(data.get("intent") or "").strip()
        intent = Intent(intent_s)
        meta: dict[str, Any] = {}
        if intent == Intent.ENQUEUE_GOAL:
            meta["goal"] = str(data.get("goal") or text).strip()
        elif intent == Intent.NEED_PREP:
            qs = data.get("questions") or [text]
            meta["questions"] = [str(q) for q in qs][:5]
        elif intent == Intent.PROPOSE_HANDOFF:
            meta["role"] = str(data.get("role") or "pm")
            meta["reason"] = str(data.get("reason") or text)[:300]
        else:
            meta["topic"] = text
        return intent, meta
    except Exception:  # noqa: BLE001
        logger.info("model classify unavailable; using heuristics")
        return None


def resolve_intent(text: str, conn: Any = None) -> tuple[Intent, dict[str, Any]]:
    if conn is not None:
        modeled = _try_model_classify(text, conn)
        if modeled is not None:
            return modeled
    return classify_intent(text)


def _judgment_claims(statements: list[str]) -> list[Claim]:
    """Label conversational prose as judgment (ADR-0021) for the gate."""
    return [Claim(statement=s, is_judgment=True) for s in statements if s.strip()]


def _send_grounded_or_direct(
    conn: Any,
    client: Any,
    notifier: Any,
    *,
    text: str,
    to: Optional[str],
    originating: str = "role/spokesman",
) -> None:
    """Prefer grounding gate; fall back to direct send for judgments / no notifier."""
    if notifier is not None and conn is not None:
        try:
            from .grounding_gate import relay_claims

            result = relay_claims(
                conn,
                notifier,
                kind="inform",
                originating_identity=originating,
                claims=_judgment_claims([text]),
            )
            # Capturing clients (web) also need the text on the messaging client.
            if getattr(client, "replies", None) is not None or not result.get("relayed"):
                client.send_text(text, to=to)
            return
        except Exception:  # noqa: BLE001
            logger.warning("grounding relay failed; sending direct")
    client.send_text(text, to=to)


def handle_conversation(
    settings: Any,
    client: Any,
    connect: ConnectFn,
    msg: Any,
    *,
    notifier: Any = None,
) -> ConverseOutcome:
    """Run free-text converse for one inbound message."""
    text = (msg.text or "").strip()
    to = getattr(msg, "sender", None) or None
    workstream = getattr(settings, "workstream", None) or "productivity"

    if _END_HANDOFF_RE.match(text):
        try:
            from .handoff import end_handoff

            conn = connect()
            try:
                ended = end_handoff(conn, sink=DbEventSink(conn))
            finally:
                _close(conn)
            reply = (
                "Handoff ended. You're back with Spokesman only."
                if ended
                else "No active handoff to end."
            )
            client.send_text(reply, to=to)
            return ConverseOutcome(intent=Intent.ANSWER, replies=[reply], meta={"ended": bool(ended)})
        except Exception:  # noqa: BLE001
            logger.exception("end handoff failed")
            client.send_text("Could not end handoff; try again shortly.", to=to)
            return ConverseOutcome(intent=Intent.ANSWER, replies=[], meta={"error": "db"})

    conn = None
    sink: EventSink = NullEventSink()
    try:
        conn = connect()
        sink = DbEventSink(conn)
    except Exception:  # noqa: BLE001
        logger.warning("DB unavailable for converse; limited mode")

    if conn is not None:
        emit_body_free(
            sink,
            type=EVENT_HUMAN_MESSAGE,
            workstream=workstream,
            payload={"channel": getattr(settings, "channel", "web"), "chars": len(text)},
        )

    intent, meta = resolve_intent(text, conn=conn)

    try:
        if intent == Intent.ANSWER:
            return _do_answer(conn, client, notifier, settings, text, to, meta, sink, workstream)
        if intent == Intent.ENQUEUE_GOAL:
            return _do_enqueue_goal(conn, client, text, to, meta, sink, workstream)
        if intent == Intent.NEED_PREP:
            return _do_need_prep(conn, client, text, to, meta, sink, workstream)
        if intent == Intent.PROPOSE_HANDOFF:
            return _do_propose_handoff(conn, client, to, meta, sink, workstream)
    finally:
        if conn is not None:
            _close(conn)

    reply = "I heard you, but could not act — try again shortly."
    client.send_text(reply, to=to)
    return ConverseOutcome(intent=intent, replies=[reply], meta=meta)


def _do_answer(
    conn: Any,
    client: Any,
    notifier: Any,
    settings: Any,
    text: str,
    to: Optional[str],
    meta: dict,
    sink: EventSink,
    workstream: str,
) -> ConverseOutcome:
    if conn is None:
        from .state import status_summary

        summary = status_summary(settings)
        reply = f"{summary}\n\n(Asked: {text[:200]})"
        client.send_text(reply, to=to)
        return ConverseOutcome(intent=Intent.ANSWER, replies=[reply], meta=meta)

    ctx: StudioContext = context_for_answer(conn)
    reply = (
        f"*Spokesman*\n{ctx.render_brief()}\n\n"
        f"_Re: {text[:240]}_"
    )
    # Active handoff: still Spokesman answers unless this is a relay path.
    _send_grounded_or_direct(conn, client, notifier, text=reply, to=to)
    return ConverseOutcome(
        intent=Intent.ANSWER,
        replies=[reply],
        meta={**meta, "refreshed_at": ctx.refreshed_at},
    )


def _do_enqueue_goal(
    conn: Any,
    client: Any,
    text: str,
    to: Optional[str],
    meta: dict,
    sink: EventSink,
    workstream: str,
) -> ConverseOutcome:
    goal = str(meta.get("goal") or text).strip()
    if conn is None:
        reply = "Runtime DB unreachable — could not enqueue that for the PM. Try again shortly."
        client.send_text(reply, to=to)
        return ConverseOutcome(intent=Intent.ENQUEUE_GOAL, replies=[reply], meta={"ok": False})

    task = enqueue_task(
        conn,
        workstream=workstream,
        type=PM_TICK_TYPE,
        payload={"goal": goal, "source": "spokesman", "kind": "human_goal"},
        priority=50,
    )
    emit_body_free(
        sink,
        type=EVENT_HUMAN_GOAL,
        workstream=workstream,
        task_id=task.id,
        payload={"task_id": str(task.id), "source": "spokesman"},
    )
    reply = (
        f"Got it — queued for the PM (task {str(task.id)[:8]}…). "
        "I'll update you as it plans."
    )
    client.send_text(reply, to=to)
    return ConverseOutcome(
        intent=Intent.ENQUEUE_GOAL,
        replies=[reply],
        meta={"ok": True, "task_id": str(task.id), "goal": goal},
    )


def _do_need_prep(
    conn: Any,
    client: Any,
    text: str,
    to: Optional[str],
    meta: dict,
    sink: EventSink,
    workstream: str,
) -> ConverseOutcome:
    questions = list(meta.get("questions") or [text])
    if conn is None:
        reply = "On it — but DB is down, so I can't dig deeper yet."
        client.send_text(reply, to=to)
        return ConverseOutcome(intent=Intent.NEED_PREP, replies=[reply], meta={"ok": False})

    task = enqueue_task(
        conn,
        workstream=workstream,
        type=SPOKESMAN_PREP_TYPE,
        payload={
            "questions": questions[:5],
            "priority": "high",
            "source": "spokesman",
        },
        priority=100,
    )
    topic = questions[0][:120] if questions else "that"
    reply = f"On it — checking {topic}… I'll follow up shortly."
    client.send_text(reply, to=to)
    return ConverseOutcome(
        intent=Intent.NEED_PREP,
        replies=[reply],
        meta={"ok": True, "task_id": str(task.id)},
    )


def _do_propose_handoff(
    conn: Any,
    client: Any,
    to: Optional[str],
    meta: dict,
    sink: EventSink,
    workstream: str,
) -> ConverseOutcome:
    if conn is None:
        reply = "Can't propose a handoff without the runtime DB."
        client.send_text(reply, to=to)
        return ConverseOutcome(intent=Intent.PROPOSE_HANDOFF, replies=[reply], meta={"ok": False})

    role = str(meta.get("role") or "pm")
    reason = str(meta.get("reason") or "Extended specialist discussion.")
    handoff, approval = propose_handoff(
        conn, role=role, reason=reason, workstream=workstream, sink=sink
    )
    reply = (
        f"This may need a direct {role.upper()} back-and-forth. "
        f"Approve handoff? Reply: approve {str(approval.id)[:8]}… "
        f"(full id: {approval.id}) — or deny {approval.id}"
    )
    client.send_text(reply, to=to)
    return ConverseOutcome(
        intent=Intent.PROPOSE_HANDOFF,
        replies=[reply],
        meta={
            "ok": True,
            "handoff_id": str(handoff.id),
            "approval_id": str(approval.id),
            "role": role,
        },
    )


def run_prep_task(conn: Any, task: Any, sink: EventSink) -> dict[str, Any]:
    """Worker handler for ``spokesman.prep`` — refresh cache + emit ready event."""
    payload = dict(task.payload or {})
    questions = payload.get("questions") or []
    notes = [f"Prep for: {q}" for q in questions[:5] if isinstance(q, str)]
    ctx = refresh_prep_cache(conn, notes=notes)
    emit_body_free(
        sink,
        type=EVENT_SPOKESMAN_PREP_READY,
        workstream=getattr(task, "workstream", None) or "productivity",
        task_id=getattr(task, "id", None),
        payload={
            "task_id": str(getattr(task, "id", "")),
            "n_questions": len(questions),
            "refreshed_at": ctx.refreshed_at,
        },
    )
    follow_up = (
        f"*Follow-up*\n{ctx.render_brief()}\n\n"
        f"_Prepared for: {'; '.join(str(q) for q in questions[:2]) or 'your ask'}_"
    )
    return {
        "ok": True,
        "follow_up": follow_up,
        "refreshed_at": ctx.refreshed_at,
        "n_questions": len(questions),
    }


def maybe_tag_handoff_reply(conn: Any, role: str, text: str) -> str:
    """Format a specialist relay if a handoff is active for ``role``."""
    from .handoff import active_handoff

    active = active_handoff(conn)
    if active and active.role == role:
        return format_handoff_relay(role, text)
    return text


def _close(conn: object) -> None:
    try:
        conn.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
