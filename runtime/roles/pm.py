"""PM role — understand → confidence-gate → decompose into work items (ADR-0003).

The PM is the **supervisor** (architecture §3, ADR-0003): it owns completion and
is the only role that plans. It never does the work itself — it acts through
nothing but ``call_model`` and the task queue (CLAUDE.md invariants 1 & 2). On a
``pm.tick`` pulse it:

1. resolves a **goal** (task payload → objective string → default);
2. obtains a **structured plan** by calling ``call_model(role="pm",
   task_type="plan", …)`` and PARSING its structured (JSON) output into a
   :class:`Plan` — restated goal, measurable success criteria, a self-scored
   ``confidence`` in ``[0,1]``, a ``feasible`` flag + reason, and a list of
   :class:`WorkItem` (the decomposition). Parsing is defensive: unparseable model
   output degrades to a safe low-confidence fallback (never a crash);
3. runs the **confidence gate** (ADR-0003) on that plan:

   - **not feasible** → *push back* (a first-class output): emit ``pm.pushback``
     and raise a 🛑 human approval (an objective/scope concern, ADR-0006); enqueue
     NO work.
   - **confidence below ``PM_CONFIDENCE_THRESHOLD``** (or nothing to decompose) →
     *clarify*: emit ``pm.needs_clarification`` instead of executing; enqueue no
     work.
   - **otherwise** → *decompose*: enqueue ONE work task per :class:`WorkItem`
     (each carrying its own concrete, checkable criterion + marker so the Verifier
     still checks a real artifact), then emit ``pm.planned`` with the item COUNT +
     task ids (never any secret / prompt text).

The plan is obtained through the single instrumented call site, so it is routed,
costed, and logged like any model call; keyless it returns the deterministic
:func:`runtime.model.providers.dryrun.build_dry_run_plan` decomposition, and a
real model wired later returns the same schema — no PM code change.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from ..approvals import request_approval as _request_approval
from ..enforce import EventSink, NullEventSink
from ..model.call import call_model as _call_model
from ..model.providers.dryrun import PLAN_GOAL_OPT
from ..model.registry import Registry
from ..models import Task, make_event
from ..skills import SkillRegistry, compose_prompt
from .lessons import inject_lessons

log = logging.getLogger("runtime.roles.pm")

#: Role events for the three confidence-gate outcomes.
EVENT_PM_PLANNED = "pm.planned"
EVENT_PM_NEEDS_CLARIFICATION = "pm.needs_clarification"
EVENT_PM_PUSHBACK = "pm.pushback"

#: Default work-task type for an item that does not name one (Executor + Verifier
#: service any ``work.*`` task). A non-``work.*`` type is coerced to this so the
#: worker always has a handler.
DEFAULT_WORK_TASK_TYPE = "work.task"

#: Back-compat alias — historically the PM enqueued a single ``work.demo`` task.
WORK_TASK_TYPE = DEFAULT_WORK_TASK_TYPE

#: Env var + default for the confidence gate threshold (ADR-0003 self-score).
CONFIDENCE_THRESHOLD_ENV = "PM_CONFIDENCE_THRESHOLD"
DEFAULT_CONFIDENCE_THRESHOLD = 0.6

#: Tier shown on the 🛑 pushback approval (ADR-0006 "approve (blocks)" class).
PUSHBACK_TIER = "🛑"

#: Default objective when a pulse carries no explicit goal.
DEFAULT_OBJECTIVE = "Prove the studio operates end-to-end in dry-run."

# Base persona prompt. On-demand skills (ADR-0008) are composed in on top of this
# when a SkillRegistry is supplied — see `_compose_plan_prompt`. The prompt asks
# for the structured JSON contract so a real model returns the same schema the
# keyless dry-run provider does.
_PLAN_PROMPT = (
    "You are the studio PM (the supervisor). Understand the goal, then produce a "
    "PLAN as a single JSON object with these fields: restated_goal (string), "
    "success_criteria (array of concrete, checkable strings), confidence (number "
    "0..1 = your self-scored confidence you can deliver this), feasible (boolean) "
    "and reason (string; if not feasible, why), and work_items (array). Each "
    "work_item is an object with title, type (default \"work.task\"), instructions, "
    "and success_criterion (concrete + independently checkable). Decompose the goal "
    "into the work items needed. If the requirement is unreasonable or out of "
    "scope, set feasible=false and say why. Goal: {goal}"
)

#: Selection query for the PM's planning skills (matches `define-success-criteria`).
_PM_SKILL_QUERY = "pm plan success criteria confidence gate"


def _confidence_threshold() -> float:
    """The confidence gate threshold (env ``PM_CONFIDENCE_THRESHOLD`` or default)."""
    raw = os.environ.get(CONFIDENCE_THRESHOLD_ENV, "").strip()
    if not raw:
        return DEFAULT_CONFIDENCE_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "invalid %s=%r; using default %s",
            CONFIDENCE_THRESHOLD_ENV, raw, DEFAULT_CONFIDENCE_THRESHOLD,
        )
        return DEFAULT_CONFIDENCE_THRESHOLD


def _compose_plan_prompt(goal: str, skills: Optional[SkillRegistry]) -> str:
    """Base plan prompt + any relevant, REVIEWED skills (on-demand injection).

    With no registry the prompt is the inline base (behavior-preserving). With
    one, only skills relevant to PM planning are selected and only the reviewed
    ones are injected (:func:`runtime.skills.compose_prompt`).
    """
    base = _PLAN_PROMPT.format(goal=goal)
    if skills is None:
        return base
    return compose_prompt(base, skills.select(_PM_SKILL_QUERY))


# --- The structured plan contract -------------------------------------------


class WorkItem(BaseModel):
    """One unit of work the PM decomposes the goal into.

    ``success_criterion`` is per-item, concrete + independently checkable so the
    Verifier can judge it against a real artifact. ``marker`` is the token the
    artifact must contain for the deterministic evidence gate; when omitted the PM
    derives a unique one per item at enqueue time.
    """

    title: str = ""
    type: str = DEFAULT_WORK_TASK_TYPE
    instructions: str = ""
    success_criterion: str = ""
    marker: Optional[str] = None


class Plan(BaseModel):
    """The PM's structured plan (the model's parsed planning output).

    ``confidence`` is the PM's self-score in ``[0,1]`` (clamped on parse);
    ``feasible`` + ``reason`` carry a pushback signal; ``work_items`` is the
    decomposition the PM enqueues when the gate opens.
    """

    restated_goal: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    feasible: bool = True
    reason: str = ""
    work_items: list[WorkItem] = Field(default_factory=list)


class PlanResult(BaseModel):
    """What the PM decided on a tick (returned to the worker for the task result).

    ``decision`` is one of ``"planned"`` | ``"needs_clarification"`` |
    ``"pushback"``. Only ``planned`` enqueues work; the others record why the PM
    did not execute. Carries ids/counts, never secret/prompt text.
    """

    goal: str
    decision: str
    restated_goal: str = ""
    confidence: float = 0.0
    feasible: bool = True
    reason: str = ""
    work_item_count: int = 0
    work_task_ids: list[str] = Field(default_factory=list)
    approval_id: Optional[str] = None


def _resolve_goal(task: Task) -> str:
    payload = task.payload or {}
    for key in ("goal", "objective"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_OBJECTIVE


def _extract_json(text: str) -> Optional[dict]:
    """Parse the first JSON object out of ``text`` defensively.

    Tries a strict parse first, then the outermost ``{...}`` slice (models often
    wrap JSON in prose/code fences). Returns ``None`` if nothing parses — the
    caller degrades to a low-confidence fallback rather than crashing.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _parse_plan(text: str, goal: str) -> Plan:
    """Parse a model completion into a :class:`Plan`, defensively.

    Unparseable / schema-invalid output → a SAFE low-confidence fallback (feasible
    but ``confidence=0.0``) so the gate routes to clarification instead of
    executing a plan we could not read. Confidence is clamped to ``[0,1]``.
    """
    obj = _extract_json(text)
    if obj is None:
        log.warning("PM plan output was not parseable JSON; using low-confidence fallback")
        return Plan(restated_goal=goal, confidence=0.0, feasible=True,
                    reason="model output was not parseable; low confidence")
    try:
        plan = Plan.model_validate(obj)
    except ValidationError:
        log.warning("PM plan output did not match the schema; using low-confidence fallback")
        return Plan(restated_goal=goal, confidence=0.0, feasible=True,
                    reason="model output did not match the plan schema; low confidence")
    # Clamp the self-score into range regardless of what the model emitted.
    plan.confidence = max(0.0, min(1.0, float(plan.confidence)))
    if not plan.restated_goal:
        plan.restated_goal = goal
    return plan


def _obtain_plan(
    conn: Any,
    task: Task,
    goal: str,
    sink: EventSink,
    *,
    registry: Optional[Registry],
    skills: Optional[SkillRegistry],
    call_model: Callable[..., Any],
) -> Plan:
    """Assemble the prompt, call the model, and parse a :class:`Plan` from it.

    The prompt is the PM persona + relevant reviewed skills (ADR-0008) + any
    durable lessons prior retros distilled for this workstream (ADR-0003,
    auto-injected). The ``plan_goal`` option lets the keyless dry-run provider
    return the deterministic structured plan; a real model reads the goal from the
    prompt and returns the same schema.
    """
    prompt = _compose_plan_prompt(goal, skills)
    prompt = inject_lessons(
        prompt, conn, task.workstream, f"{goal} success criteria decomposition"
    )
    completion = call_model(
        role="pm",
        task_type="plan",
        messages=[{"role": "user", "content": prompt}],
        quality="high",
        registry=registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
        **{PLAN_GOAL_OPT: goal},
    )
    return _parse_plan(getattr(completion, "text", ""), goal)


def run_pm_tick(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    enqueue: Callable[..., Task] = None,  # type: ignore[assignment]
    call_model: Callable[..., Any] = _call_model,
    request_approval: Callable[..., Any] = _request_approval,
) -> PlanResult:
    """Service one ``pm.tick``: understand → confidence-gate → decompose (ADR-0003).

    ``conn`` is passed to ``enqueue`` / ``call_model`` / ``request_approval`` (real
    DB) and to ``call_model`` for token accounting. ``enqueue``, ``call_model`` and
    ``request_approval`` are injectable so a test drives every gate branch with
    fakes and no database (defaults are the real functions).
    """
    if enqueue is None:  # deferred default to avoid an import cycle at module load
        from ..tasks import enqueue_task
        enqueue = enqueue_task
    sink = sink or NullEventSink()
    goal = _resolve_goal(task)

    plan = _obtain_plan(
        conn, task, goal, sink,
        registry=registry, skills=skills, call_model=call_model,
    )
    threshold = _confidence_threshold()

    # --- Confidence gate (ADR-0003) -----------------------------------------

    # 1. Not feasible → push back (a first-class output). Raise a 🛑 approval so a
    #    human decides on the objective/scope concern; enqueue NO work.
    if not plan.feasible:
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_PM_PUSHBACK,
                task_id=task.id,
                payload={
                    "goal": goal,
                    "confidence": plan.confidence,
                    "reason": plan.reason or "requirement judged infeasible",
                },
            )
        )
        approval = request_approval(
            conn,
            task_id=task.id,
            role="pm",
            tool="pm.plan",
            capabilities=[],
            tier=PUSHBACK_TIER,
            reason=plan.reason or "PM pushback: requirement judged infeasible / out of scope",
            sink=sink,
            workstream=task.workstream,
        )
        return PlanResult(
            goal=goal, decision="pushback", restated_goal=plan.restated_goal,
            confidence=plan.confidence, feasible=False, reason=plan.reason,
            approval_id=str(approval.id) if approval is not None else None,
        )

    # 2. Below threshold (or nothing to decompose) → clarify instead of executing.
    if plan.confidence < threshold or not plan.work_items:
        reason = (
            plan.reason or "no decomposable work items"
            if not plan.work_items
            else f"confidence {plan.confidence:.2f} below threshold {threshold:.2f}"
        )
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_PM_NEEDS_CLARIFICATION,
                task_id=task.id,
                payload={
                    "goal": goal,
                    "confidence": plan.confidence,
                    "threshold": threshold,
                    "reason": reason,
                },
            )
        )
        return PlanResult(
            goal=goal, decision="needs_clarification", restated_goal=plan.restated_goal,
            confidence=plan.confidence, feasible=True, reason=reason,
        )

    # 3. Decompose → enqueue ONE work task per work item, each carrying its own
    #    concrete criterion + marker (the Executor/Verifier contract).
    work_task_ids: list[str] = []
    for i, item in enumerate(plan.work_items, start=1):
        marker = (item.marker or "").strip() or f"studio-ok:{task.id}:{i}"
        wtype = item.type if item.type.startswith("work.") else DEFAULT_WORK_TASK_TYPE
        criterion = item.success_criterion or f"The artifact contains the marker {marker!r}."
        work = enqueue(
            conn,
            workstream=task.workstream,
            type=wtype,
            payload={
                "goal": item.instructions or goal,
                "criterion": criterion,
                "marker": marker,
                "title": item.title,
                "item_index": i,
                "item_count": len(plan.work_items),
                "attempt": 1,
            },
            priority=task.priority,
        )
        work_task_ids.append(str(work.id))

    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_PM_PLANNED,
            task_id=task.id,
            payload={
                "goal": goal,
                "confidence": plan.confidence,
                "work_item_count": len(work_task_ids),
                "work_task_ids": work_task_ids,
            },
        )
    )
    return PlanResult(
        goal=goal, decision="planned", restated_goal=plan.restated_goal,
        confidence=plan.confidence, feasible=True, reason=plan.reason,
        work_item_count=len(work_task_ids), work_task_ids=work_task_ids,
    )
