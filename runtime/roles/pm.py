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
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from ..approvals import request_approval as _request_approval
from ..crossworkstream import (
    EVENT_REQUEST_ACCEPTED,
    EVENT_REQUEST_DECLINED,
    EVENT_REQUEST_ESCALATED,
    EVENT_REQUEST_NEEDS_CLARIFICATION,
    EVENT_REQUEST_UNDER_REVIEW,
    FEATURE_REQUEST_TYPE,
    STATUS_ACCEPTED,
    STATUS_DECLINED,
    STATUS_ESCALATED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_UNDER_REVIEW,
    FeatureRequest,
    emit_request_event,
    set_request_status,
)
from ..enforce import EventSink, NullEventSink
from ..event_types import (
    EVENT_PM_NEEDS_CLARIFICATION,
    EVENT_PM_PLANNED,
    EVENT_PM_PUSHBACK,
)
from ..model.call import call_model as _call_model
from ..model.providers.dryrun import PLAN_GOAL_OPT
from ..model.registry import Registry
from ..models import Task, TaskStatus, make_event
from ..skills import SkillRegistry
from ..task_state import assert_acyclic
from .lessons import recall_lesson_texts
from .prompt import compose_role_prompt

log = logging.getLogger("runtime.roles.pm")

#: The three confidence-gate outcome events (``pm.planned`` /
#: ``pm.needs_clarification`` / ``pm.pushback``) are imported from the canonical
#: :mod:`runtime.event_types`.

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
    "success_criterion (concrete + independently checkable), and depends_on (array "
    "of the 1-based indices of the other work items that must finish first — [] if "
    "it can run in parallel). Decompose the goal into the work items needed and set "
    "depends_on so independent items run in parallel and dependent ones wait. If the "
    "requirement is unreasonable or out of scope, set feasible=false and say why. "
    "Goal: {goal}"
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


def _compose_plan_prompt(
    goal: str,
    skills: Optional[SkillRegistry],
    lessons: Optional[list[str]] = None,
    *,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> str:
    """Assemble the PM's plan prompt through the shared role assembler.

    Layers base persona → charter → overlay → relevant reviewed skills → recalled
    lessons, via :func:`runtime.roles.prompt.compose_role_prompt`. With no registry,
    no lessons, and no charter/overlay the prompt is the inline base
    (behavior-preserving). ``charter``/``overlay`` are the vertical's config-driven
    framing (default ``None``).
    """
    base = _PLAN_PROMPT.format(goal=goal)
    selected = skills.select(_PM_SKILL_QUERY) if skills is not None else None
    return compose_role_prompt(
        base,
        workstream_charter=charter,
        role_overlay=overlay,
        skills=selected,
        lessons=lessons,
    )


# --- The structured plan contract -------------------------------------------


class WorkItem(BaseModel):
    """One unit of work the PM decomposes the goal into.

    ``success_criterion`` is per-item, concrete + independently checkable so the
    Verifier can judge it against a real artifact. ``marker`` is the token the
    artifact must contain for the deterministic evidence gate; when omitted the PM
    derives a unique one per item at enqueue time. ``depends_on`` lists the 1-based
    indices of the *other* work items this one depends on (its prerequisites) — the
    edges that tell the fleet what is parallel vs sequential (ADR-0015).
    """

    title: str = ""
    type: str = DEFAULT_WORK_TASK_TYPE
    instructions: str = ""
    success_criterion: str = ""
    marker: Optional[str] = None
    depends_on: list[int] = Field(default_factory=list)


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


def _topo_order(edges: dict[int, list[int]]) -> list[int]:
    """Return item indices in dependency order (prerequisites first).

    ``edges[i]`` are the prerequisites of item ``i``. Assumes acyclic (the caller
    runs :func:`assert_acyclic` first). Stable: ties break by ascending index, so a
    fully-independent plan keeps its natural 1..n order.
    """
    order: list[int] = []
    placed: set[int] = set()

    def visit(n: int, stack: set[int]) -> None:
        if n in placed:
            return
        stack.add(n)
        for dep in edges.get(n, []):
            if dep not in stack:  # cycles already rejected; guard defensively
                visit(dep, stack)
        stack.discard(n)
        if n not in placed:
            placed.add(n)
            order.append(n)

    for node in sorted(edges):
        visit(node, set())
    return order


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
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> Plan:
    """Assemble the prompt, call the model, and parse a :class:`Plan` from it.

    The prompt is the PM persona + charter/overlay (vertical config) + relevant
    reviewed skills (ADR-0008) + any durable lessons prior retros distilled for
    this workstream (ADR-0003, auto-injected) — all layered by the shared
    :func:`runtime.roles.prompt.compose_role_prompt`. The ``plan_goal`` option lets
    the keyless dry-run provider return the deterministic structured plan; a real
    model reads the goal from the prompt and returns the same schema.
    """
    lessons = recall_lesson_texts(
        conn, task.workstream, f"{goal} success criteria decomposition"
    )
    prompt = _compose_plan_prompt(
        goal, skills, lessons, charter=charter, overlay=overlay
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
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
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
        charter=charter, overlay=overlay,
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
    #    concrete criterion + marker (the Executor/Verifier contract) and its
    #    dependency edges. The PM's index-based edges are validated acyclic and
    #    mapped to the created task ids so dependents wait for their prerequisites
    #    to merge (ADR-0015).
    n = len(plan.work_items)
    # Sanitize edges to valid 1-based, non-self indices; reject cycles up front.
    edges: dict[int, list[int]] = {}
    for i, item in enumerate(plan.work_items, start=1):
        edges[i] = sorted({d for d in item.depends_on if 1 <= d <= n and d != i})
    assert_acyclic(edges)  # DependencyCycle on a cyclic / self-referential plan

    order = _topo_order(edges)  # prerequisites first, so their ids exist on enqueue
    id_by_index: dict[int, str] = {}
    for i in order:
        item = plan.work_items[i - 1]
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
                "item_count": n,
                "attempt": 1,
            },
            priority=task.priority,
            depends_on=[UUID(id_by_index[d]) for d in edges[i]],
        )
        id_by_index[i] = str(work.id)
    # Report ids in the PM's original item order (stable, human-readable).
    work_task_ids = [id_by_index[i] for i in range(1, n + 1)]

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


# --- Cross-workstream request intake / triage (ADR-0018 coordination) --------
#
# The RECEIVING PM's added path: it evaluates a `feature_request` addressed to
# ITS workstream through its OWN success lens and either accepts (decomposes into
# work items linked to the request), declines (pushback is first-class), asks for
# clarification, or escalates a portfolio/resource decision as a 🛑 approval.
# This is additive to `run_pm_tick` — the pm.tick planning path is untouched.

#: The four triage outcomes a receiving PM may return.
TRIAGE_ACCEPT = "accept"
TRIAGE_DECLINE = "decline"
TRIAGE_CLARIFY = "needs_clarification"
TRIAGE_ESCALATE = "escalate"
_TRIAGE_DECISIONS = frozenset({TRIAGE_ACCEPT, TRIAGE_DECLINE, TRIAGE_CLARIFY, TRIAGE_ESCALATE})

#: Tier for the 🛑 approval an escalation raises (ADR-0006 "approve (blocks)").
ESCALATION_TIER = "🛑"


class TriageDecision(BaseModel):
    """A receiving PM's verdict on a feature request (its own-lens evaluation).

    ``decision`` is one of ``accept`` / ``decline`` / ``needs_clarification`` /
    ``escalate``. On ``accept`` the PM may supply ``work_items`` (its chosen
    decomposition); if it does not, the request's ``success_criteria`` are
    decomposed one-per-item. ``reason`` records the rationale for pushback /
    clarification / escalation (carried on the event, never a body).
    """

    decision: str
    reason: str = ""
    work_items: list[WorkItem] = Field(default_factory=list)


class TriageResult(BaseModel):
    """What the receiving PM decided on a request (ids/counts only, no bodies)."""

    request_id: str
    from_workstream: str
    to_workstream: str
    decision: str
    reason: str = ""
    work_item_count: int = 0
    work_task_ids: list[str] = Field(default_factory=list)
    approval_id: Optional[str] = None


def _default_triage(task: Task, request: FeatureRequest) -> TriageDecision:
    """The keyless default lens: accept a well-specified request, else clarify.

    A request with no ``success_criteria`` **and** no ``desired_capability`` gives
    the receiver nothing checkable to build against → ask for clarification. A real
    model (wired later) can return any of the four via the ``evaluate`` seam; this
    deterministic default keeps the contract exercisable dry-run/keyless.
    """
    if not request.success_criteria and not request.desired_capability.strip():
        return TriageDecision(
            decision=TRIAGE_CLARIFY,
            reason="no success criteria or desired capability to build against",
        )
    return TriageDecision(decision=TRIAGE_ACCEPT)


def _ensure_in_progress(conn: Any, task: Task, worker_id: str) -> None:
    """Pick the request task up (up_for_grabs → claimed → in_progress) as the PM.

    Guarded + idempotent: if the task is already past these states each hop
    no-ops, so triage is safe to run on a freshly-submitted or already-claimed
    request. Uses the single guarded :func:`runtime.tasks.transition`.
    """
    from ..tasks import transition

    transition(
        conn, task.id, TaskStatus.CLAIMED,
        agent_id=worker_id, agent_type="pm", claimed_by=worker_id,
        set_claimed_at=True, set_heartbeat=True,
        expected_from=TaskStatus.UP_FOR_GRABS,
    )
    transition(
        conn, task.id, TaskStatus.IN_PROGRESS,
        agent_id=worker_id, agent_type="pm",
        expected_from=TaskStatus.CLAIMED,
    )


def _decompose_request(
    conn: Any,
    task: Task,
    request: FeatureRequest,
    items: list[WorkItem],
    enqueue: Callable[..., Task],
) -> list[str]:
    """Enqueue one ``up_for_grabs`` work item per criterion, linked to the request.

    Each work item lands on the RECEIVING workstream's board (``task.workstream``)
    carrying the requester's ``success_criterion`` as its checkable criterion (the
    requester defines "done"; the receiver owns "how") plus a back-link
    (``request_id`` / ``from_workstream``) so the accepted work is traceable to the
    ask. Runs after :func:`_ensure_in_progress`; returns the created task ids.
    """
    n = len(items)
    ids: list[str] = []
    for i, item in enumerate(items, start=1):
        marker = (item.marker or "").strip() or f"request-ok:{task.id}:{i}"
        wtype = item.type if item.type.startswith("work.") else DEFAULT_WORK_TASK_TYPE
        criterion = item.success_criterion or f"The artifact contains the marker {marker!r}."
        work = enqueue(
            conn,
            workstream=task.workstream,
            type=wtype,
            payload={
                "goal": item.instructions or request.desired_capability or request.title,
                "criterion": criterion,
                "marker": marker,
                "title": item.title or request.title,
                "request_id": str(task.id),
                "from_workstream": request.from_workstream,
                "item_index": i,
                "item_count": n,
                "attempt": 1,
            },
            priority=task.priority,
        )
        ids.append(str(work.id))
    return ids


def _items_for_accept(decision: TriageDecision, request: FeatureRequest) -> list[WorkItem]:
    """The decomposition to enqueue on accept: the PM's own, else one per criterion.

    Falls back to a single item built from ``desired_capability`` when the request
    carries no explicit ``success_criteria`` (the accept branch is only reached
    when there IS something checkable — see :func:`_default_triage`).
    """
    if decision.work_items:
        return decision.work_items
    if request.success_criteria:
        return [
            WorkItem(
                title=f"{request.title}: criterion {i}",
                success_criterion=crit,
                instructions=request.desired_capability or request.problem,
            )
            for i, crit in enumerate(request.success_criteria, start=1)
        ]
    return [
        WorkItem(
            title=request.title,
            success_criterion=request.desired_capability,
            instructions=request.desired_capability or request.problem,
        )
    ]


def triage_request(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    decision: Optional[str] = None,
    reason: str = "",
    work_items: Optional[list[WorkItem]] = None,
    receiving_workstream: Optional[str] = None,
    worker_id: str = "pm",
    evaluate: Optional[Callable[[Task, FeatureRequest], TriageDecision]] = None,
    enqueue: Callable[..., Task] = None,  # type: ignore[assignment]
    request_approval: Callable[..., Any] = _request_approval,
) -> TriageResult:
    """Receiving-PM intake for a ``feature_request`` addressed to its workstream.

    The PM evaluates the request through **its own** success lens and takes one of
    four first-class paths (pushback/decline is as valid as accept):

    - **accept** → decompose into ``up_for_grabs`` work items linked to the request
      (the request's ``success_criteria`` become the items' criteria) + emit
      ``request.accepted``; the request task is driven to ``merged``.
    - **decline** → emit ``request.declined`` (reason); enqueue NO work; the request
      task is ``abandoned``.
    - **needs_clarification** → emit ``request.needs_clarification`` back to the
      requester; the request task returns to ``up_for_grabs`` to await a reply.
    - **escalate** → emit ``request.escalated`` + raise a 🛑 ``request_approval``
      (a portfolio/resource decision — either side may escalate); the request task
      is parked ``blocked`` on that approval.

    Scope is respected: the task's ``workstream`` is the receiver, and if
    ``receiving_workstream`` is supplied it must match (else ``ValueError`` — a PM
    never triages another workstream's request). The decision comes from an
    explicit ``decision=`` argument, else the injectable ``evaluate`` lens, else the
    keyless default. ``enqueue`` / ``request_approval`` are injectable for tests.
    All ``request.*`` events carry ids/decision/reason only — never request bodies.
    """
    if task.type != FEATURE_REQUEST_TYPE:
        raise ValueError(
            f"triage_request expects a {FEATURE_REQUEST_TYPE!r} task, got {task.type!r}"
        )
    if receiving_workstream is not None and receiving_workstream != task.workstream:
        raise ValueError(
            f"scope violation: {receiving_workstream!r} may not triage a request "
            f"addressed to {task.workstream!r}"
        )
    if enqueue is None:  # deferred default to avoid an import cycle at module load
        from ..tasks import enqueue_task
        enqueue = enqueue_task
    sink = sink or NullEventSink()

    request = FeatureRequest.from_task(task)
    to_ws, from_ws = task.workstream, request.from_workstream

    # The PM picks the request up and puts it under review (identity-only event).
    _ensure_in_progress(conn, task, worker_id)
    set_request_status(conn, task.id, STATUS_UNDER_REVIEW)
    emit_request_event(
        sink, type=EVENT_REQUEST_UNDER_REVIEW, request_id=task.id,
        from_workstream=from_ws, to_workstream=to_ws, status=STATUS_UNDER_REVIEW,
    )

    # Resolve the verdict: explicit arg > injected lens > keyless default.
    if decision is not None:
        verdict = TriageDecision(
            decision=decision, reason=reason, work_items=work_items or [],
        )
    elif evaluate is not None:
        verdict = evaluate(task, request)
    else:
        verdict = _default_triage(task, request)
    if verdict.decision not in _TRIAGE_DECISIONS:
        raise ValueError(
            f"invalid triage decision {verdict.decision!r} "
            f"(one of {sorted(_TRIAGE_DECISIONS)})"
        )
    verdict_reason = reason or verdict.reason

    from ..tasks import block_task, complete_task, transition

    # --- accept → decompose + link, then merge the request ------------------
    if verdict.decision == TRIAGE_ACCEPT:
        items = _items_for_accept(verdict, request)
        work_task_ids = _decompose_request(conn, task, request, items, enqueue)
        set_request_status(
            conn, task.id, STATUS_ACCEPTED, reason=verdict_reason,
            work_task_ids=work_task_ids,
        )
        emit_request_event(
            sink, type=EVENT_REQUEST_ACCEPTED, request_id=task.id,
            from_workstream=from_ws, to_workstream=to_ws, status=STATUS_ACCEPTED,
            decision=STATUS_ACCEPTED, reason=verdict_reason,
            work_item_count=len(work_task_ids), work_task_ids=work_task_ids,
        )
        complete_task(
            conn, task.id, status=TaskStatus.MERGED,
            result={"decision": STATUS_ACCEPTED, "work_task_ids": work_task_ids},
        )
        return TriageResult(
            request_id=str(task.id), from_workstream=from_ws, to_workstream=to_ws,
            decision=STATUS_ACCEPTED, reason=verdict_reason,
            work_item_count=len(work_task_ids), work_task_ids=work_task_ids,
        )

    # --- decline → reason, NO work, abandon the request ---------------------
    if verdict.decision == TRIAGE_DECLINE:
        set_request_status(conn, task.id, STATUS_DECLINED, reason=verdict_reason)
        emit_request_event(
            sink, type=EVENT_REQUEST_DECLINED, request_id=task.id,
            from_workstream=from_ws, to_workstream=to_ws, status=STATUS_DECLINED,
            decision=STATUS_DECLINED, reason=verdict_reason,
        )
        complete_task(
            conn, task.id, status=TaskStatus.ABANDONED,
            result={"decision": STATUS_DECLINED, "reason": verdict_reason},
        )
        return TriageResult(
            request_id=str(task.id), from_workstream=from_ws, to_workstream=to_ws,
            decision=STATUS_DECLINED, reason=verdict_reason,
        )

    # --- needs_clarification → back to the requester; re-queue to await reply
    if verdict.decision == TRIAGE_CLARIFY:
        set_request_status(
            conn, task.id, STATUS_NEEDS_CLARIFICATION, reason=verdict_reason,
        )
        emit_request_event(
            sink, type=EVENT_REQUEST_NEEDS_CLARIFICATION, request_id=task.id,
            from_workstream=from_ws, to_workstream=to_ws,
            status=STATUS_NEEDS_CLARIFICATION,
            decision=STATUS_NEEDS_CLARIFICATION, reason=verdict_reason,
        )
        # Return the request to the board so a clarified re-submission can be
        # re-triaged (in_progress → up_for_grabs is the documented recovery edge).
        transition(
            conn, task.id, TaskStatus.UP_FOR_GRABS,
            expected_from=TaskStatus.IN_PROGRESS, clear_claim=True,
        )
        return TriageResult(
            request_id=str(task.id), from_workstream=from_ws, to_workstream=to_ws,
            decision=STATUS_NEEDS_CLARIFICATION, reason=verdict_reason,
        )

    # --- escalate → 🛑 approval (portfolio/resource); park the request blocked
    escalate_reason = verdict_reason or "cross-workstream portfolio/resource decision"
    approval = request_approval(
        conn,
        task_id=task.id,
        role="pm",
        tool="request.escalate",
        capabilities=[],
        tier=ESCALATION_TIER,
        reason=escalate_reason,
        sink=sink,
        workstream=to_ws,
    )
    approval_id = str(approval.id) if approval is not None else None
    set_request_status(conn, task.id, STATUS_ESCALATED, reason=escalate_reason)
    emit_request_event(
        sink, type=EVENT_REQUEST_ESCALATED, request_id=task.id,
        from_workstream=from_ws, to_workstream=to_ws, status=STATUS_ESCALATED,
        decision=STATUS_ESCALATED, reason=escalate_reason, approval_id=approval_id,
    )
    if approval_id is not None:
        block_task(conn, task.id, approval_id=approval.id, reason=escalate_reason)
    return TriageResult(
        request_id=str(task.id), from_workstream=from_ws, to_workstream=to_ws,
        decision=STATUS_ESCALATED, reason=escalate_reason, approval_id=approval_id,
    )
