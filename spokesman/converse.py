"""Spokesman agent — model-first human interface (ADR-0026).

The model interprets the stakeholder message first. Studio actions
(``studio_status``, ``enqueue_goal``, ``request_prep``, ``propose_handoff``,
``end_handoff``) are **tools** the agent may call — they never replace
understanding the prompt.

Keyword shortcuts (``status`` / ``approve`` / ``deny`` / ``decide``) remain a
fast path in ``app.handle_inbound_command`` for SMS muscle memory; free text
always enters this agent loop.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from runtime.enforce import DbEventSink, EventSink, NullEventSink
from runtime.event_types import EVENT_HUMAN_GOAL, EVENT_HUMAN_MESSAGE
from runtime.model import call_model
from runtime.scheduler import PM_TICK_TYPE
from runtime.tasks import enqueue_task

from .context import (
    SPOKESMAN_PREP_TYPE,
    context_for_answer,
    emit_body_free,
    refresh_prep_cache,
)
from .handoff import end_handoff, propose_handoff

logger = logging.getLogger(__name__)

ConnectFn = Callable[[], Any]

MAX_AGENT_ROUNDS = 4
TOOL_NAMES = frozenset(
    {
        "studio_status",
        "enqueue_goal",
        "request_prep",
        "propose_handoff",
        "end_handoff",
        "list_pending_approvals",
        "resolve_approval",
    }
)

SYSTEM_PROMPT = """\
You are Spokesman — the sole default human interface for AI Studio (a local-first
venture-studio agent OS). You speak naturally with the stakeholder. You know the
studio through tools; you do not invent facts.

Rules:
1. Read and interpret the human's message FIRST. Respond like a competent aide.
2. Use tools when you need live studio data or must take an action. Do not dump
   raw status unless the human asked for it or you need it to answer.
3. Never claim you did something (queued work, approved, etc.) unless a tool
   result confirms it.
4. Keep replies concise (SMS-friendly). Prefer one short paragraph + bullets.
5. Coordination with other agents is ONLY via tools (enqueue / prep / handoff) —
   you never call other agents directly.
6. When the human verbally approves or denies a pending 🛑 request ("yes", "go
   ahead", "approve that", "no", …), call resolve_approval — do NOT only chat.
   Keyword shortcuts `approve <id>` / `deny <id>` also work; free text must use
   the tool. Prefer an explicit approval_id; if omitted and exactly one pending
   approval exists, resolve that one.

Available tools (call by name with JSON args):
- studio_status() — live open-task / approval / decision snapshot.
- enqueue_goal(goal: string) — queue a requirement for the PM (pm.tick).
- request_prep(questions: string[]) — high-priority prep dig; you'll follow up.
- propose_handoff(role: string, reason: string) — ask human to approve a
  specialist back-and-forth (pm|critic|reviewer|researcher).
- end_handoff() — end an active specialist handoff.
- list_pending_approvals() — pending 🛑 ids + short reasons (for verbal approve).
- resolve_approval(decision: string, approval_id?: string) — approve/deny a
  pending approval (decision: approve|deny|yes|no). Omitting approval_id is OK
  only when exactly one is pending; otherwise list them and ask which.

Response format — return ONLY a single JSON object, no markdown fences:
{"tool_calls":[{"name":"<tool>","args":{...}}, ...], "reply":"<text or null>"}

- If you need tools first: set tool_calls (1+), reply null (or a brief ack).
- When ready to answer the human: tool_calls [] and a non-null reply.
- You may combine: run tools and also set a short ack reply the same turn.
"""


@dataclass
class ConverseOutcome:
    """Result of one agent session with the stakeholder."""

    intent: str = "converse"
    replies: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ToolCtx:
    settings: Any
    connect: ConnectFn
    conn: Any
    sink: EventSink
    workstream: str


def _close(conn: object) -> None:
    try:
        conn.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _parse_agent_json(raw: str) -> dict[str, Any]:
    """Extract the agent JSON object from a model completion."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("agent output is not an object")
    return data


def _run_tool(name: str, args: dict[str, Any], ctx: _ToolCtx) -> dict[str, Any]:
    """Execute one Spokesman tool; return a JSON-serializable result."""
    args = args if isinstance(args, dict) else {}
    if name == "studio_status":
        if ctx.conn is None:
            from .state import status_summary

            return {"ok": True, "text": status_summary(ctx.settings)}
        try:
            brief = context_for_answer(ctx.conn).render_brief()
            return {"ok": True, "text": brief}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    if name == "enqueue_goal":
        goal = str(args.get("goal") or "").strip()
        if not goal:
            return {"ok": False, "error": "missing goal"}
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        task = enqueue_task(
            ctx.conn,
            workstream=ctx.workstream,
            type=PM_TICK_TYPE,
            payload={"goal": goal, "source": "spokesman", "kind": "human_goal"},
            priority=50,
        )
        emit_body_free(
            ctx.sink,
            type=EVENT_HUMAN_GOAL,
            workstream=ctx.workstream,
            task_id=task.id,
            payload={"task_id": str(task.id), "source": "spokesman"},
        )
        return {"ok": True, "task_id": str(task.id), "goal": goal}

    if name == "request_prep":
        questions = args.get("questions") or []
        if isinstance(questions, str):
            questions = [questions]
        questions = [str(q).strip() for q in questions if str(q).strip()][:5]
        if not questions:
            return {"ok": False, "error": "missing questions"}
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        task = enqueue_task(
            ctx.conn,
            workstream=ctx.workstream,
            type=SPOKESMAN_PREP_TYPE,
            payload={
                "questions": questions,
                "priority": "high",
                "source": "spokesman",
            },
            priority=100,
        )
        return {"ok": True, "task_id": str(task.id), "questions": questions}

    if name == "propose_handoff":
        role = str(args.get("role") or "pm").strip().lower() or "pm"
        reason = str(args.get("reason") or "Specialist discussion needed.").strip()[:500]
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        handoff, approval = propose_handoff(
            ctx.conn,
            role=role,
            reason=reason,
            workstream=ctx.workstream,
            sink=ctx.sink,
        )
        return {
            "ok": True,
            "handoff_id": str(handoff.id),
            "approval_id": str(approval.id),
            "role": role,
            "hint": f"Human must reply: approve {approval.id}",
        }

    if name == "end_handoff":
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        ended = end_handoff(ctx.conn, sink=ctx.sink)
        if ended is None:
            return {"ok": False, "error": "no active handoff"}
        return {"ok": True, "handoff_id": str(ended.id), "role": ended.role}

    if name == "list_pending_approvals":
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        from .runtime_bridge import _pending_approvals_real

        rows = _pending_approvals_real(ctx.conn)
        items = [
            {
                "approval_id": str(r["id"]),
                "tool": r.get("tool"),
                "role": r.get("role"),
                "tier": r.get("tier"),
                "reason": (r.get("reason") or "")[:240],
                "task_id": str(r["task_id"]) if r.get("task_id") else None,
            }
            for r in rows[:20]
        ]
        return {"ok": True, "count": len(items), "approvals": items}

    if name == "resolve_approval":
        if ctx.conn is None:
            return {"ok": False, "error": "db unavailable"}
        from .runtime_bridge import resolve
        from .handoff import activate_handoff_for_approval

        decision = str(args.get("decision") or "").strip().lower()
        if not decision:
            return {"ok": False, "error": "missing decision (approve|deny)"}
        approval_id = str(args.get("approval_id") or "").strip() or None
        if not approval_id:
            from .runtime_bridge import _pending_approvals_real

            pending = _pending_approvals_real(ctx.conn)
            if len(pending) == 1:
                approval_id = str(pending[0]["id"])
            elif not pending:
                return {"ok": False, "error": "no pending approvals"}
            else:
                return {
                    "ok": False,
                    "error": "ambiguous: multiple pending approvals",
                    "approvals": [
                        {
                            "approval_id": str(r["id"]),
                            "tool": r.get("tool"),
                            "reason": (r.get("reason") or "")[:120],
                        }
                        for r in pending[:10]
                    ],
                }
        resolver = f"spokesman:converse"
        try:
            approval = resolve(ctx.conn, approval_id, decision, resolver)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if approval is None:
            return {
                "ok": False,
                "error": "approval not found or already resolved",
                "approval_id": approval_id,
            }
        handoff_id = None
        if approval.status == "approved":
            try:
                handoff = activate_handoff_for_approval(ctx.conn, approval.id)
                if handoff is not None:
                    handoff_id = str(handoff.id)
            except Exception:  # noqa: BLE001
                logger.exception("handoff activate failed for %s", approval_id)
        return {
            "ok": True,
            "approval_id": str(approval.id),
            "status": approval.status,
            "handoff_id": handoff_id,
            "resumed": True,
        }

    return {"ok": False, "error": f"unknown tool {name!r}"}


def _agent_turn(
    messages: list[dict[str, Any]],
    *,
    conn: Any,
    workstream: str,
    user_text: str,
) -> dict[str, Any]:
    """One model turn; returns parsed agent JSON (or a safe conversational fallback)."""
    completion = call_model(
        "spokesman",
        "converse",
        messages,
        quality="standard",
        conn=conn,
        sink=NullEventSink(),
        workstream=workstream,
        spokesman_user=user_text,
        spokesman_messages=messages,
    )
    raw = (completion.text or "").strip()
    try:
        return _parse_agent_json(raw)
    except Exception:  # noqa: BLE001 - model drifted; still talk to the human
        logger.warning("spokesman agent JSON parse failed; using raw text fallback")
        if raw.startswith("[dry-run:"):
            # Should not happen when dry-run spokesman_user is wired; belt+suspenders.
            return {
                "tool_calls": [],
                "reply": (
                    f"I heard you: {user_text[:400]}. "
                    "(Model dry-run — wire a provider key for full replies.)"
                ),
            }
        # Prefer treating free text as the reply rather than dying silently.
        return {"tool_calls": [], "reply": raw[:2000] or "Sorry — I blanked. Try again?"}


def handle_conversation(
    settings: Any,
    client: Any,
    connect: ConnectFn,
    msg: Any,
    *,
    notifier: Any = None,
    session_key: Optional[str] = None,
) -> ConverseOutcome:
    """Run the Spokesman agent on one inbound free-text message.

    ``session_key`` scopes conversation memory (migration 0018): the web client
    passes a stable per-conversation id; SMS/WhatsApp fall back to the sender id.
    Prior turns for this session are loaded and threaded into the model prompt so
    the Spokesman is no longer amnesiac across turns, and both this human turn and
    the reply are persisted. Degrade-safe: if the DB is down there is simply no
    history and nothing is persisted — the Spokesman still replies (ADR-0026: this
    surface must work when other things are down).
    """
    del notifier  # reserved: future grounded outbound from agent claims
    text = (msg.text or "").strip()
    to = getattr(msg, "sender", None) or None
    workstream = getattr(settings, "workstream", None) or "productivity"
    # Session key: explicit (web) → sender id (SMS/WhatsApp) → stable fallback so a
    # keyless/older client is still ONE coherent session rather than amnesiac.
    session = (session_key or "").strip() or (getattr(msg, "sender", None) or "").strip() or "web"

    conn = None
    sink: EventSink = NullEventSink()
    try:
        conn = connect()
        sink = DbEventSink(conn)
    except Exception:  # noqa: BLE001
        logger.warning("DB unavailable for spokesman agent; tools that need DB will fail")

    if conn is not None:
        emit_body_free(
            sink,
            type=EVENT_HUMAN_MESSAGE,
            workstream=workstream,
            payload={
                "channel": getattr(settings, "channel", "web"),
                "chars": len(text),
            },
        )

    ctx = _ToolCtx(
        settings=settings,
        connect=connect,
        conn=conn,
        sink=sink,
        workstream=workstream,
    )

    # Thread bounded per-session history (ADR-0013) so turn N sees turns 1..N-1.
    # history_messages is degrade-safe (returns [] if the DB is unavailable).
    from .conversation import history_messages, record_turn

    history = history_messages(conn, session)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": text},
    ]
    replies: list[str] = []
    tool_trace: list[dict[str, Any]] = []
    final_reply: Optional[str] = None

    try:
        for _round in range(MAX_AGENT_ROUNDS):
            parsed = _agent_turn(
                messages, conn=conn, workstream=workstream, user_text=text
            )
            tool_calls = parsed.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                tool_calls = []
            reply = parsed.get("reply")
            if isinstance(reply, str) and reply.strip():
                final_reply = reply.strip()
                replies.append(final_reply)

            if not tool_calls:
                break

            results = []
            for call in tool_calls[:5]:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "").strip()
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                if name not in TOOL_NAMES:
                    result = {"ok": False, "error": f"unknown tool {name!r}"}
                else:
                    result = _run_tool(name, args, ctx)
                tool_trace.append({"name": name, "args": args, "result": result})
                results.append({"name": name, "result": result})

            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {"tool_calls": tool_calls, "reply": reply},
                        ensure_ascii=False,
                    ),
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Tool results (JSON). Continue: more tools or final reply.\n"
                        + json.dumps(results, ensure_ascii=False)
                    ),
                }
            )
        else:
            if final_reply is None:
                final_reply = (
                    "I started looking into that but hit my step limit — "
                    "ask me to continue or try a narrower question."
                )
                replies.append(final_reply)

        if final_reply is None:
            # Tools ran with no prose — synthesize a minimal human-facing line.
            if tool_trace:
                final_reply = _summarize_tools(tool_trace)
            else:
                final_reply = "I'm here — what should we focus on?"
            replies.append(final_reply)

        # Persist this exchange (human turn + Spokesman reply) so the NEXT message
        # in this session can see it. Body lives ONLY here (invariant 6), never on
        # an event. Best-effort: a persistence failure must never break the reply.
        if conn is not None:
            try:
                record_turn(conn, session, "human", text)
                record_turn(conn, session, "spokesman", replies[-1])
            except Exception:  # noqa: BLE001 - memory is a nicety, the reply is not
                logger.warning("record_turn failed; reply sent, turn not remembered")

        # Send once (last reply is the one for the human this turn).
        client.send_text(replies[-1], to=to)
        return ConverseOutcome(
            intent="converse",
            replies=[replies[-1]],
            meta={
                "ok": True,
                "rounds": min(MAX_AGENT_ROUNDS, len(tool_trace) + 1),
                "tools": [t["name"] for t in tool_trace],
                "tool_trace": tool_trace,
            },
        )
    finally:
        if conn is not None:
            _close(conn)


def _summarize_tools(tool_trace: list[dict[str, Any]]) -> str:
    parts = []
    for t in tool_trace:
        name = t.get("name")
        result = t.get("result") or {}
        if name == "studio_status" and result.get("ok"):
            parts.append(str(result.get("text") or "").strip())
        elif name == "enqueue_goal" and result.get("ok"):
            parts.append(
                f"Queued for the PM (task {str(result.get('task_id', ''))[:8]}…)."
            )
        elif name == "request_prep" and result.get("ok"):
            parts.append("On it — digging in; I'll follow up.")
        elif name == "propose_handoff" and result.get("ok"):
            parts.append(
                f"Proposed handoff to {result.get('role')}. "
                f"{result.get('hint') or ''}"
            )
        elif name == "end_handoff" and result.get("ok"):
            parts.append("Handoff ended — back to Spokesman only.")
        elif name == "list_pending_approvals" and result.get("ok"):
            n = int(result.get("count") or 0)
            parts.append(f"{n} pending approval(s)." if n else "No pending approvals.")
        elif name == "resolve_approval" and result.get("ok"):
            parts.append(
                f"Approval {str(result.get('approval_id', ''))[:8]}… "
                f"{result.get('status')} — queued work can resume."
            )
        elif not result.get("ok"):
            parts.append(f"{name} failed: {result.get('error')}")
    return "\n\n".join(p for p in parts if p) or "Done."


def run_prep_task(conn: Any, task: Any, sink: EventSink) -> dict[str, Any]:
    """Worker handler for ``spokesman.prep`` — refresh cache + emit ready event."""
    from runtime.event_types import EVENT_SPOKESMAN_PREP_READY

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
    return {
        "ok": True,
        "follow_up": f"*Follow-up*\n{ctx.render_brief()}",
        "refreshed_at": ctx.refreshed_at,
        "n_questions": len(questions),
    }


def build_dry_run_spokesman_turn(
    user_text: str,
    *,
    messages: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Deterministic agent JSON for keyless/dry-run (mirrors PM plan_goal).

    Interprets the user message lightly so dry-run still behaves like an agent
    (tools when appropriate, otherwise a normal conversational reply) — never a
    status paste glued under ``_Re: …``.
    """
    # Follow-up after tools: produce a final human reply from tool results.
    if messages:
        for msg in reversed(messages):
            content = str(msg.get("content") or "")
            if content.startswith("Tool results"):
                return _dry_run_reply_after_tools(content)

    text = (user_text or "").strip()
    lower = text.lower()

    # Memory demonstration (keyless): if the human asks for their name and an
    # EARLIER turn in this session stated it, recall it from threaded history.
    # This proves conversation memory end-to-end without a real provider key; a
    # real model does the same from the same threaded messages.
    if re.search(r"\b(what'?s|what is)\b.*\bmy name\b", lower) or lower in {
        "what's my name?",
        "what is my name?",
        "what's my name",
    }:
        recalled = _recall_name_from_history(messages)
        if recalled:
            return {"tool_calls": [], "reply": f"Your name is {recalled}."}
        return {
            "tool_calls": [],
            "reply": "I don't have your name yet — tell me and I'll remember it.",
        }

    # Action-shaped → use tools (model-first dry-run still "chooses" tools).
    if re.search(
        r"\b(please |pls )?(build|implement|create|ship|fix|add|enqueue|"
        r"i want|i need|we need|work on)\b",
        text,
        re.I,
    ) and not text.endswith("?"):
        return {
            "tool_calls": [{"name": "enqueue_goal", "args": {"goal": text}}],
            "reply": None,
        }
    if re.search(r"\b(dig into|look up|check the (logs|budget|db))\b", text, re.I):
        return {
            "tool_calls": [{"name": "request_prep", "args": {"questions": [text]}}],
            "reply": "On it — checking that now.",
        }
    if re.search(r"\b(talk (directly )?to|hand ?off|speak with)\b", text, re.I):
        role = "pm"
        for candidate in ("critic", "reviewer", "researcher", "pm"):
            if re.search(rf"\b{candidate}\b", text, re.I):
                role = candidate
                break
        return {
            "tool_calls": [
                {
                    "name": "propose_handoff",
                    "args": {"role": role, "reason": text[:300]},
                }
            ],
            "reply": None,
        }
    if re.search(r"^(end handoff|stop handoff)\b", text, re.I):
        return {"tool_calls": [{"name": "end_handoff", "args": {}}], "reply": None}
    # Verbal approve/deny without the keyword shortcut → resolve_approval tool.
    if re.search(
        r"\b(approve|approved|deny|denied|reject)\b",
        lower,
    ) or re.search(
        r"^(yes|yeah|yep|go ahead|sounds good|do it|lgtm|nope|no)\b",
        lower,
    ):
        decision = "deny" if re.search(
            r"\b(deny|denied|reject|nope)\b", lower
        ) or re.match(r"^no\b", lower) else "approve"
        m = re.search(
            r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
            lower,
        )
        args: dict[str, Any] = {"decision": decision}
        if m:
            args["approval_id"] = m.group(1)
        return {
            "tool_calls": [{"name": "resolve_approval", "args": args}],
            "reply": None,
        }
    if re.search(
        r"\b(status|how'?s?\b|how (is|are) (things|we|the studio)|"
        r"what('s| is) (blocked|open|pending)|any updates?|"
        r"studio looking)\b",
        lower,
    ) or lower in {"status", "hi", "hello", "hey"}:
        if lower in {"hi", "hello", "hey"}:
            return {
                "tool_calls": [],
                "reply": (
                    "Hey — Spokesman here. Ask me anything about the studio, "
                    "give a requirement to queue for the PM, or say status if you "
                    "want a snapshot."
                ),
            }
        return {
            "tool_calls": [{"name": "studio_status", "args": {}}],
            "reply": None,
        }

    short = text if len(text) <= 280 else text[:277] + "…"
    return {
        "tool_calls": [],
        "reply": (
            f"Got it — \"{short}\". I can pull live studio status, queue work for "
            "the PM, dig deeper, or propose a specialist handoff. What would you "
            "like me to do with this?"
        ),
    }


def _recall_name_from_history(
    messages: Optional[list[dict[str, Any]]],
) -> Optional[str]:
    """Scan threaded history for an earlier ``my name is <X>`` (dry-run recall)."""
    for msg in messages or []:
        if str(msg.get("role")) != "user":
            continue
        m = re.search(
            r"\bmy name'?s?\s+(?:is\s+)?([A-Z][a-zA-Z'\-]{1,30})",
            str(msg.get("content") or ""),
            re.I,
        )
        if m:
            return m.group(1).strip().capitalize()
    return None


def _dry_run_reply_after_tools(tool_results_content: str) -> dict[str, Any]:
    """Second-turn dry-run: turn tool JSON into a short human reply."""
    try:
        blob = tool_results_content.split("\n", 1)[1]
        results = json.loads(blob)
    except Exception:  # noqa: BLE001
        return {
            "tool_calls": [],
            "reply": "I ran the tools — ask if you want more detail.",
        }
    parts: list[str] = []
    for item in results if isinstance(results, list) else []:
        name = item.get("name")
        result = item.get("result") or {}
        if name == "studio_status" and result.get("ok"):
            parts.append(str(result.get("text") or "").strip())
        elif name == "enqueue_goal" and result.get("ok"):
            tid = str(result.get("task_id") or "")[:8]
            parts.append(
                f"Queued that for the PM (task {tid}…). I'll update you as it plans."
            )
        elif name == "request_prep" and result.get("ok"):
            parts.append("Prep is running — I'll follow up when it's ready.")
        elif name == "propose_handoff" and result.get("ok"):
            parts.append(
                f"I proposed a {result.get('role', 'pm').upper()} handoff. "
                f"{result.get('hint') or 'Approve the pending approval to activate.'}"
            )
        elif name == "end_handoff" and result.get("ok"):
            parts.append("Handoff ended. You're back with me only.")
        elif result.get("ok") is False:
            parts.append(f"Couldn't complete {name}: {result.get('error')}")
    reply = "\n\n".join(p for p in parts if p) or "Done — anything else?"
    return {"tool_calls": [], "reply": reply}
