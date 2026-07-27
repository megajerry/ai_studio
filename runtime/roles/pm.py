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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from .. import adaptive
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
    EVENT_PM_CONSENSUS,
    EVENT_PM_NEEDS_CLARIFICATION,
    EVENT_PM_PLANNED,
    EVENT_PM_PUSHBACK,
    EVENT_TASK_REPLAN_ESCALATED,
    EVENT_TASK_REPLANNED,
)
from ..model.call import call_model as _call_model
from ..model.providers.dryrun import PLAN_GOAL_OPT
from ..model.registry import Registry
from ..models import Task, TaskStatus, make_event
from ..skills import SkillRegistry, emit_skill_applied
from ..task_state import assert_acyclic
from ..trajectory import add_step, close_trajectory, start_trajectory
from .capacity_steward import CAPACITY_REVIEW_TYPE
from .critic import CRITIC_ESCALATE, CRITIC_REVISE, Critique
from .failure_analyst import FAILURE_ANALYST_TASK_TYPES
from .lessons import recall_lesson_texts
from .prompt import compose_role_prompt
from .skill_lifecycle import SKILL_LIFECYCLE_TASK_TYPES
from .sourcing import SOURCING_TASK_TYPES

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

#: Env var + default for the bounded PM↔Critic consensus rounds (ADR-0019). This
#: caps the consult→revise loop so it can NEVER run unbounded; the last allowed
#: round, if the Critic still blocks, escalates a genuine disagreement to a human.
PM_CRITIC_ROUNDS_ENV = "PM_CRITIC_ROUNDS"
DEFAULT_PM_CRITIC_ROUNDS = 2

#: The two consensus outcomes (emitted on ``pm.consensus``).
CONSENSUS_AGREED = "consensus"
CONSENSUS_ESCALATED = "escalated"

#: Default objective when a pulse carries no explicit goal.
DEFAULT_OBJECTIVE = "Prove the studio operates end-to-end in dry-run."

#: Work-task type dispatched to :func:`run_pm_replan` (ADR-0023, R2). A ``task.stuck``
#: signal is turned into ONE ``replan`` task per stuck task by the queue consumer
#: (:func:`runtime.scheduler.dispatch_replans`) — never an agent-to-agent call — and
#: the worker routes it here.
REPLAN_TASK_TYPE = "replan"

#: Env var + default for the max replan DEPTH (ADR-0023, R2 bound). A stuck task
#: carries its ``replan_depth`` in its payload; each re-decomposition stamps its
#: subtasks with ``depth + 1``. Once a stuck task's depth reaches this cap the PM
#: STOPS re-decomposing and escalates to a human 🛑 — so a subtask that itself keeps
#: getting stuck can NEVER recurse into an infinite replan. Kept small on purpose.
PM_MAX_REPLAN_DEPTH_ENV = "PM_MAX_REPLAN_DEPTH"
DEFAULT_MAX_REPLAN_DEPTH = 2

#: The two replan outcomes returned by :func:`run_pm_replan`.
REPLAN_DECOMPOSED = "replanned"
REPLAN_ESCALATED = "escalated"


# --- Trajectory recording (ADR-0020) — observe-only, DB-outage-safe ----------
#
# The PM's reasoning is persisted as a first-class trajectory (ADR-0020): it is the
# most critical, least-reversible role, so how it reached a decision must be
# replayable. Recording is STRICTLY observe-only — it never changes a PM decision —
# and DEGRADES GRACEFULLY (ADR-0017): with no `conn` (the fake-queue unit paths) or
# on any trajectory-write failure it logs + returns None so the PM's core function
# (plan → gate → decompose) NEVER blocks or crashes. Every write goes through the
# single guarded writer in :mod:`runtime.trajectory` (no ad-hoc SQL here).


def _traj_start(conn: Any, workstream: str, goal: str) -> Optional[UUID]:
    """Open the PM's reasoning trajectory, or ``None`` if unavailable/degraded."""
    if conn is None:
        return None
    try:
        return start_trajectory(conn, "pm", workstream, goal)
    except Exception:  # pragma: no cover - defensive: never let recording break the PM
        log.warning("PM trajectory start failed; proceeding without a trajectory", exc_info=True)
        return None


def _traj_step(conn: Any, tid: Optional[UUID], step_type: str, summary: str, **kw: Any) -> None:
    """Append one reasoning step, degrading to a no-op on any failure (ADR-0017)."""
    if conn is None or tid is None:
        return
    try:
        add_step(conn, tid, step_type, summary, **kw)
    except Exception:  # pragma: no cover - defensive: recording is never load-bearing
        log.warning("PM trajectory step %r failed; proceeding", step_type, exc_info=True)


def _traj_close(conn: Any, tid: Optional[UUID], *, outcome_summary: Optional[str] = None) -> None:
    """Close the trajectory, degrading to a no-op on any failure (ADR-0017)."""
    if conn is None or tid is None:
        return
    try:
        close_trajectory(conn, tid, outcome_summary=outcome_summary)
    except Exception:  # pragma: no cover - defensive
        log.warning("PM trajectory close failed; proceeding", exc_info=True)

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


def _critic_rounds() -> int:
    """Bounded PM↔Critic rounds (env ``PM_CRITIC_ROUNDS`` or default; min 1)."""
    raw = os.environ.get(PM_CRITIC_ROUNDS_ENV, "").strip()
    if not raw:
        return DEFAULT_PM_CRITIC_ROUNDS
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning(
            "invalid %s=%r; using default %s",
            PM_CRITIC_ROUNDS_ENV, raw, DEFAULT_PM_CRITIC_ROUNDS,
        )
        return DEFAULT_PM_CRITIC_ROUNDS


def _max_replan_depth() -> int:
    """Max replan depth (env ``PM_MAX_REPLAN_DEPTH`` or default; min 0).

    0 disables re-decomposition entirely (the first stuck escalation goes straight
    to a human 🛑); higher values allow that many rounds of re-decomposition before
    the bound trips. Kept small so a repeatedly-stuck task cannot replan forever.
    """
    raw = os.environ.get(PM_MAX_REPLAN_DEPTH_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_REPLAN_DEPTH
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "invalid %s=%r; using default %s",
            PM_MAX_REPLAN_DEPTH_ENV, raw, DEFAULT_MAX_REPLAN_DEPTH,
        )
        return DEFAULT_MAX_REPLAN_DEPTH


# --- PM exposes + commissions the specialist roles (ADR-0031) ----------------
#
# Several specialist roles (Sourcing, Failure-pattern analyst, Skill-lifecycle,
# Capacity Steward) have worker handlers but historically NO producer — nothing ever
# enqueued their task type, so they never ran. The stakeholder direction (2026-07-27)
# is that all roles are EXPOSED to the PM and the PM can ENQUEUE tasks for them. The
# worker's dispatch is now role-agnostic (ADR-0031); this is the producer half: the
# PM knows the roles exist (they are listed in its plan prompt via role_catalog_note)
# and commissions one by JUDGMENT (never a cron) via enqueue_role_task. Coordination
# stays queue-only (invariant 1) — the PM drops a task; the worker claims + dispatches.

#: The specialist role task types the PM may commission by judgment (ADR-0031), each
#: mapped to a one-line "when to use it" note. This is the SINGLE catalog the PM
#: exposes: it drives both the plan-prompt role menu (:func:`role_catalog_note`) and
#: the enqueue helper's validation (:func:`enqueue_role_task`). Adding a
#: PM-commissionable role is a one-line entry here. These are NOT on autonomous crons —
#: the PM enqueues one only when its judgment says the studio would benefit (the
#: human/PM stays in the loop; the workstream is not self-sufficient yet).
PM_ROLE_TASK_TYPES: dict[str, str] = {
    SOURCING_TASK_TYPES[0]:
        "Sourcing — research/refresh the model catalog + pricing and propose a "
        "reviewable registry update (ADR-0005). Commission when models look stale or "
        "a cheaper/better option may exist.",
    FAILURE_ANALYST_TASK_TYPES[0]:
        "Failure-pattern analyst — detect a RECURRING failure in the event log and "
        "propose a durable fix framed as an experiment (ADR-0023 R3). Commission when "
        "you see repeated errors/stalls on a workstream.",
    SKILL_LIFECYCLE_TASK_TYPES[0]:
        "Skill-lifecycle — judge LIVE skills' efficacy and propose a human-gated "
        "deprecation/revision for underperformers (ADR-0024 P4). Commission "
        "periodically once skills have accrued applied usage.",
    CAPACITY_REVIEW_TYPE:
        "Capacity Steward — review a workstream's budget burn and flag + recommend "
        "an early action before the engine has to block (ADR-0022 C2). Commission "
        "when a workstream is burning hot or nearing its ceiling.",
}


def role_catalog_note() -> str:
    """The bulleted role-commissioning menu injected into the PM plan prompt (ADR-0031).

    One line per :data:`PM_ROLE_TASK_TYPES` entry (``- `type` — when to use it``).
    Pure text; it EXPOSES the specialist roles to the PM so the planner knows they
    exist and can enqueue them by judgment. Kept in sync with the catalog by
    construction (no drift).
    """
    return "\n".join(f"- `{t}` — {note}" for t, note in PM_ROLE_TASK_TYPES.items())


def enqueue_role_task(
    conn: Any,
    *,
    workstream: str,
    task_type: str,
    enqueue: Callable[..., Task] = None,  # type: ignore[assignment]
    payload: Optional[dict] = None,
    priority: int = 0,
) -> Task:
    """Commission ONE specialist role by enqueuing a task of its type (ADR-0031).

    The PM's bounded seam for "enqueue a task for any role": it validates ``task_type``
    against :data:`PM_ROLE_TASK_TYPES` (an unknown/unsupported type raises
    ``ValueError`` rather than dropping a task the worker would only abandon) and
    enqueues ONE ``up_for_grabs`` task through the same guarded ``enqueue`` seam the PM
    uses everywhere. Coordination stays queue-only (CLAUDE.md invariant 1): the PM
    never calls the role — it drops a task the worker later claims + dispatches via the
    role-agnostic registry (:func:`runtime.worker.resolve_handler`). Enqueues exactly
    one task and returns it (no loop). ``enqueue`` is injectable for tests.
    """
    if task_type not in PM_ROLE_TASK_TYPES:
        raise ValueError(
            f"{task_type!r} is not a PM-commissionable role task type "
            f"(one of {sorted(PM_ROLE_TASK_TYPES)})"
        )
    if enqueue is None:  # deferred default to avoid an import cycle at module load
        from ..tasks import enqueue_task
        enqueue = enqueue_task
    return enqueue(
        conn,
        workstream=workstream,
        type=task_type,
        payload=payload or {},
        priority=priority,
    )


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
        budget_aware=True,
        # ADR-0031: EXPOSE the specialist roles the PM can commission by judgment
        # (Sourcing / Failure-analyst / Skill-lifecycle / Capacity Steward), so the
        # planner knows they exist and when to enqueue one (via enqueue_role_task).
        role_catalog=role_catalog_note(),
        # The PM owns the build-vs-buy / agile-adoption operating principle
        # (ADR-0027): it weighs building in-house vs adopting a mature component
        # and stays flexible about a better paradigm/tech, changing only on clear
        # evidence (no churn). Injected into every PM plan prompt.
        strategy_aware=True,
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
    #: Id of the external-research scan this tick commissioned (ADR-0027), if the
    #: PM's budget-tuned baseline cadence said one was due; ``None`` otherwise.
    research_task_id: Optional[str] = None


# --- PM operating principle: build vs buy/borrow + agile adoption (ADR-0027) --
#
# A higher-level principle the PM OWNS — NOT a hard-coded cron. To hedge building
# in-house against buying/borrowing and stay agile about a better paradigm/tech,
# the studio must keep learning the latest industrial developments. The PM realizes
# that by owning a slow, BUDGET-TUNED BASELINE cadence of external-research scans
# (:func:`runtime.adaptive.pm_research_interval_hours`): faster with budget headroom,
# slower (but NEVER off) when starved. On each ``pm.tick`` the PM checks the cadence
# and, when a scan is DUE, commissions EXACTLY ONE bounded ``research`` task (the
# worker dispatches it to :func:`runtime.roles.researcher.run_research`).
#
# Guardrails (no churn / no loop): at most one scan per due-window — a recent scan
# (pending OR finished) suppresses a new one, so scans never STACK; a ``research``
# task enqueues nothing (no research-of-research loop, enforced in the Researcher);
# findings are ``reviewed: false`` proposals only, so adoption stays a deliberate,
# evidence-gated decision (never auto-adopt, ADR-0008). Keyless/dry-run safe and
# DB-outage safe: any failure SKIPS the pulse and NEVER crashes the tick (ADR-0017).

#: Payload marker identifying a PM-commissioned studio-level external scan.
RESEARCH_ORIGIN_EXTERNAL_SCAN = "pm_external_scan"

#: The goal/topic carried by a commissioned external-research scan.
EXTERNAL_RESEARCH_GOAL = (
    "Scan the latest industrial developments (tools, frameworks, paradigms, "
    "best practices) relevant to the studio; propose reviewable candidates to "
    "adopt or borrow. Weigh build vs. buy/borrow; do not auto-adopt."
)


def _as_aware(dt: datetime) -> datetime:
    """Treat a naive timestamp as UTC so interval math is tz-safe."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _last_research_at(conn: Any, workstream: str) -> Optional[datetime]:
    """Most recent ``research`` task creation time for the workstream, or ``None``.

    Counts ANY research task (pending or finished) so a scan already in flight still
    suppresses a duplicate (the 'don't stack' guardrail).
    """
    from .researcher import RESEARCH_TASK_TYPE  # local: avoid an import cycle

    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(created_at) AS t FROM tasks WHERE workstream = %s AND type = %s",
            (workstream, RESEARCH_TASK_TYPE),
        )
        row = cur.fetchone()
    if not getattr(conn, "autocommit", True):
        conn.commit()
    return row["t"] if row else None


def _workstream_started_at(conn: Any, workstream: str) -> Optional[datetime]:
    """Earliest task creation time for the workstream (its activity start), or ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min(created_at) AS t FROM tasks WHERE workstream = %s",
            (workstream,),
        )
        row = cur.fetchone()
    if not getattr(conn, "autocommit", True):
        conn.commit()
    return row["t"] if row else None


def _maybe_commission_research(
    conn: Any,
    task: Task,
    *,
    enqueue: Callable[..., Task],
    now: Optional[datetime] = None,
    interval_hours: Optional[float] = None,
) -> Optional[str]:
    """Commission ONE external-research scan iff the PM's cadence says it's DUE (ADR-0027).

    Returns the new ``research`` task id when one is enqueued, else ``None``. Pure
    orchestration side-action of a ``pm.tick`` — it is NOT recorded as a plan
    reasoning step. NEVER raises: with no ``conn`` (fake-queue unit paths) or on any
    failure (DB down, budget read error) it logs + returns ``None`` so the pm.tick
    core (plan → gate → decompose) is never blocked or crashed (ADR-0017).

    Dueness: the interval is the budget-tuned baseline cadence (hours; faster with
    headroom, slower — never off — when starved). A scan is due when a full interval
    has elapsed since the last scan; if the workstream has NEVER been scanned it is
    due once the workstream has been active for a full interval (a warm-up so a
    brand-new workstream isn't scanned on its very first tick). ``now`` /
    ``interval_hours`` are injectable for tests.
    """
    if conn is None:
        return None
    try:
        from .researcher import RESEARCH_TASK_TYPE  # local: avoid an import cycle

        ref_now = _as_aware(now or datetime.now(timezone.utc))
        if interval_hours is None:
            # Budget is advisory here; a read failure → uncapped → fastest baseline.
            try:
                from ..budget import remaining as _budget_remaining
                headroom = _budget_remaining(conn, task.workstream)
            except Exception:
                headroom = None
            interval_hours = adaptive.pm_research_interval_hours(headroom)
        interval = timedelta(hours=max(1.0, float(interval_hours)))

        last = _last_research_at(conn, task.workstream)
        if last is not None:
            if ref_now - _as_aware(last) < interval:
                return None  # a recent scan exists → not due (don't stack)
        else:
            started = _workstream_started_at(conn, task.workstream)
            if started is None or (ref_now - _as_aware(started) < interval):
                return None  # never scanned, but not warmed up a full interval yet

        research = enqueue(
            conn,
            workstream=task.workstream,
            type=RESEARCH_TASK_TYPE,
            payload={
                "goal": EXTERNAL_RESEARCH_GOAL,
                "topic": EXTERNAL_RESEARCH_GOAL,
                "origin": RESEARCH_ORIGIN_EXTERNAL_SCAN,
                "commissioned_by": "pm",
            },
            priority=task.priority,
        )
        log.info(
            "PM commissioned external-research scan %s for %s (baseline cadence %.0fh)",
            research.id, task.workstream, float(interval_hours),
        )
        return str(research.id)
    except Exception:  # pragma: no cover - defensive: the pulse is never load-bearing
        log.warning(
            "PM research commission skipped (degraded); pm.tick continues", exc_info=True
        )
        return None


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


# --- PM↔Critic consensus loop (ADR-0019) ------------------------------------
#
# After the PM produces a feasible, confident Plan and BEFORE it decomposes /
# enqueues, it consults the Critic (an opt-in adversarial partner). The loop is
# BOUNDED (:data:`DEFAULT_PM_CRITIC_ROUNDS`): each round the Critic critiques the
# CURRENT plan; a non-blocking critique → consensus; a blocking one → the PM
# revises and re-consults (up to the round cap); an explicit ``escalate`` — or the
# cap being reached while still blocked — escalates a genuine disagreement to the
# stakeholder (🛑). Behavior-preserving: with no ``critic`` wired the whole loop is
# skipped and the PM behaves exactly as before.


def _plan_facts(plan: Plan, threshold: float) -> dict:
    """The plan's STRUCTURED FACTS for the Critic — numbers/flags only (no bodies).

    Everything here is a count or flag safe to compute a fact-based critique from;
    no instruction, criterion, or goal text is included, so nothing leaks even if a
    critique were (it never is) logged verbatim.
    """
    items = plan.work_items
    return {
        "kind": "plan",
        "confidence": plan.confidence,
        "threshold": threshold,
        "feasible": plan.feasible,
        "n_items": len(items),
        "n_success_criteria": len(plan.success_criteria),
        "items_missing_criterion": sum(1 for it in items if not (it.success_criterion or "").strip()),
        "items_missing_marker": sum(1 for it in items if not (it.marker or "").strip()),
        "has_dependencies": any(it.depends_on for it in items),
    }


def _revise_plan(plan: Plan) -> Plan:
    """Deterministically address the Critic's addressable gaps before re-consulting.

    Fills any missing per-item success criterion / marker and synthesizes an
    aggregate success criterion when the plan has none — the exact gaps the Critic's
    fact-based ``risk`` concerns flag. Pure (returns a copy); it never invents work
    items, so a genuine structural objection is NOT silently "fixed" and instead
    drives the loop to escalation.
    """
    revised = plan.model_copy(deep=True)
    for i, item in enumerate(revised.work_items, start=1):
        if not (item.marker or "").strip():
            item.marker = f"studio-ok:revised:{i}"
        if not (item.success_criterion or "").strip():
            item.success_criterion = f"The artifact contains the marker {item.marker!r}."
    if not revised.success_criteria and revised.work_items:
        revised.success_criteria = [
            f"All {len(revised.work_items)} work items complete and each artifact "
            "contains its success marker."
        ]
    return revised


def _run_consensus(
    conn: Any,
    task: Task,
    plan: Plan,
    threshold: float,
    sink: EventSink,
    *,
    critic: Callable[..., Critique],
    rounds: int,
    registry: Optional[Registry],
    skills: Optional[SkillRegistry],
    charter: Optional[str],
    overlay: Optional[str],
    tid: Optional[UUID] = None,
) -> tuple[Plan, str, int, int]:
    """Drive the bounded PM↔Critic consensus loop over ``plan``.

    Returns ``(plan, outcome, rounds_run, final_concern_count)`` where ``outcome`` is
    :data:`CONSENSUS_AGREED` or :data:`CONSENSUS_ESCALATED`. At most ``rounds``
    critic consults happen (hard bound — no infinite loop): each blocking round that
    is not the last revises the plan and re-consults; an ``escalate`` recommendation,
    or the last round still blocking, escalates.

    ``tid`` is the PM's open trajectory (ADR-0020): each consult is recorded by the
    Critic (via ``trajectory_id``) and each PM revision as a ``revise`` step here.
    Recording is observe-only — it changes NONE of the loop's decisions.
    """
    current = plan
    outcome = CONSENSUS_AGREED
    rounds_run = 0
    concern_count = 0
    for r in range(1, rounds + 1):
        rounds_run = r
        critique = critic(
            "plan",
            _plan_facts(current, threshold),
            sink=sink,
            conn=conn,
            task_id=task.id,
            workstream=task.workstream,
            subject_kind="plan",
            registry=registry,
            skills=skills,
            charter=charter,
            overlay=overlay,
            trajectory_id=tid,  # the Critic records the consult step (verdict + concerns)
        )
        concern_count = len(critique.concerns)
        if not critique.blocking:
            outcome = CONSENSUS_AGREED
            break
        if critique.recommendation == CRITIC_ESCALATE or r == rounds:
            # Genuine disagreement — an explicit escalate, or the bound reached
            # while still blocked. The PM could not drive to consensus → 🛑.
            outcome = CONSENSUS_ESCALATED
            break
        # Blocking + revise, and rounds remain → the PM revises, then re-consults.
        current = _revise_plan(current)
        _traj_step(
            conn, tid, "revise",
            f"revised the plan after the Critic's round {r} blocking critique",
            rationale=(
                "Addressed the Critic's addressable gaps deterministically: filled "
                "any missing per-item success criterion/marker and synthesized an "
                "aggregate success criterion where the plan had none. No work items "
                "invented — a genuine structural objection still drives escalation."
            ),
            choice=CRITIC_REVISE,
            refs={"round": r, "concern_count": concern_count},
        )
    return current, outcome, rounds_run, concern_count


def run_pm_tick(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
    critic: Optional[Callable[..., Critique]] = None,
    critic_rounds: Optional[int] = None,
    enqueue: Callable[..., Task] = None,  # type: ignore[assignment]
    call_model: Callable[..., Any] = _call_model,
    request_approval: Callable[..., Any] = _request_approval,
) -> PlanResult:
    """Service one ``pm.tick``: understand → confidence-gate → decompose (ADR-0003).

    ``conn`` is passed to ``enqueue`` / ``call_model`` / ``request_approval`` (real
    DB) and to ``call_model`` for token accounting. ``enqueue``, ``call_model`` and
    ``request_approval`` are injectable so a test drives every gate branch with
    fakes and no database (defaults are the real functions).

    ``critic`` is the opt-in PM↔Critic consensus seam (ADR-0019): when supplied
    (a :func:`runtime.roles.critic.run_critic`-shaped callable), a feasible +
    confident plan is critiqued BEFORE decompose in a BOUNDED loop
    (``critic_rounds`` / ``PM_CRITIC_ROUNDS``); an unresolved genuine disagreement
    escalates a 🛑 ``pushback`` and enqueues no work. With ``critic=None`` (default)
    the consult is skipped and behavior is unchanged.
    """
    if enqueue is None:  # deferred default to avoid an import cycle at module load
        from ..tasks import enqueue_task
        enqueue = enqueue_task
    sink = sink or NullEventSink()
    goal = _resolve_goal(task)

    # PM operating principle (ADR-0027): keep the studio learning the latest
    # industrial developments so it can hedge build vs buy/borrow and stay agile
    # about a better paradigm/tech. On this pulse, commission ONE external-research
    # scan IFF the PM's budget-tuned baseline cadence says one is due. This is a
    # side-action of the tick (not a plan step) and is degrade-safe: with no conn /
    # on any failure it is a no-op and the plan → gate → decompose core is untouched.
    research_task_id = _maybe_commission_research(conn, task, enqueue=enqueue)

    # Open ONE reasoning trajectory for this pm.tick (ADR-0020). Observe-only +
    # DB-outage-safe: with no conn / on failure `tid` is None and every _traj_*
    # below is a no-op, so the PM behaves exactly as before (behavior-preserving).
    tid = _traj_start(conn, task.workstream, goal)

    plan = _obtain_plan(
        conn, task, goal, sink,
        registry=registry, skills=skills, call_model=call_model,
        charter=charter, overlay=overlay,
    )
    # P0 attribution (ADR-0024): body-free skill.applied for the skill(s) injected
    # into the PM plan prompt above (same selection _compose_plan_prompt used).
    emit_skill_applied(
        sink, task_id=task.id, role="pm", workstream=task.workstream,
        skills=skills.select(_PM_SKILL_QUERY) if skills is not None else None,
    )
    threshold = _confidence_threshold()

    # Record what the PM understood + the decomposition it drafted (verbatim).
    _traj_step(
        conn, tid, "observe", "understood the goal and parsed the model's plan",
        rationale=(
            f"Goal: {goal}\n"
            f"Restated goal: {plan.restated_goal}\n"
            f"Feasible: {plan.feasible}; model reason: {plan.reason or '(none)'}\n"
            f"Self-scored confidence: {plan.confidence:.2f}"
        ),
        confidence=plan.confidence,
    )
    _traj_step(
        conn, tid, "plan",
        f"drafted a decomposition of {len(plan.work_items)} work item(s)",
        rationale=(
            "Success criteria:\n"
            + ("\n".join(f"- {c}" for c in plan.success_criteria) or "- (none)")
            + "\nWork items:\n"
            + ("\n".join(f"- {it.title}: {it.instructions}" for it in plan.work_items)
               or "- (none)")
        ),
        options_considered=[it.title for it in plan.work_items],
        confidence=plan.confidence,
    )

    # --- Confidence gate (ADR-0003) -----------------------------------------

    # Human already approved a prior pushback on THIS tick → consume the grant and
    # proceed (do not raise the same 🛑 again). Fingerprint matches request_approval
    # above (empty caps, empty args).
    from runtime.approvals import compute_fingerprint, consume_grant, find_grant

    pushback_fp = compute_fingerprint(
        task.id, "pm.plan", [], workstream=task.workstream, args={},
    )
    human_override = False
    if conn is not None:
        human_override = find_grant(conn, pushback_fp) is not None
        if human_override:
            consume_grant(conn, pushback_fp)
    if human_override:
        plan = plan.model_copy(update={"feasible": True})
        _traj_step(
            conn, tid, "decide",
            "human granted prior 🛑 pushback → proceed with decomposition",
            rationale="find_grant(pm.plan) hit; skipping infeasible re-pushback",
            options_considered=["decompose", "clarify", "pushback"],
            choice="decompose",
            confidence=max(plan.confidence, threshold),
        )

    # 1. Not feasible → push back (a first-class output). Raise a 🛑 approval so a
    #    human decides on the objective/scope concern; enqueue NO work.
    if not plan.feasible:
        _traj_step(
            conn, tid, "decide",
            "confidence gate: judged infeasible → push back",
            rationale=plan.reason or "requirement judged infeasible / out of scope",
            options_considered=["decompose", "clarify", "pushback"],
            choice="pushback", confidence=plan.confidence,
        )
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
        approval_id = str(approval.id) if approval is not None else None
        _traj_step(
            conn, tid, "escalate",
            "raised a 🛑 pushback approval for a human decision (infeasible)",
            rationale=plan.reason or "requirement judged infeasible / out of scope",
            choice=PUSHBACK_TIER, confidence=plan.confidence,
            refs={"approval_id": approval_id},
        )
        _traj_close(conn, tid, outcome_summary="pushback: infeasible / out of scope")
        return PlanResult(
            goal=goal, decision="pushback", restated_goal=plan.restated_goal,
            confidence=plan.confidence, feasible=False, reason=plan.reason,
            approval_id=approval_id, research_task_id=research_task_id,
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
        _traj_step(
            conn, tid, "decide",
            "confidence gate: below threshold / nothing to decompose → clarify",
            rationale=reason,
            options_considered=["decompose", "clarify", "pushback"],
            choice="needs_clarification", confidence=plan.confidence,
            refs={"threshold": threshold},
        )
        _traj_close(conn, tid, outcome_summary="needs_clarification")
        return PlanResult(
            goal=goal, decision="needs_clarification", restated_goal=plan.restated_goal,
            confidence=plan.confidence, feasible=True, reason=reason,
            research_task_id=research_task_id,
        )

    # Gate opened: the plan is feasible + confident enough to commit to.
    _traj_step(
        conn, tid, "decide",
        "confidence gate: feasible and confident → proceed to decompose",
        rationale=(
            f"confidence {plan.confidence:.2f} >= threshold {threshold:.2f}; "
            f"{len(plan.work_items)} work item(s) to decompose"
        ),
        options_considered=["decompose", "clarify", "pushback"],
        choice="proceed", confidence=plan.confidence,
        refs={"threshold": threshold},
    )

    # 2b. PM↔Critic consensus (ADR-0019, opt-in). The plan is feasible + confident;
    #     BEFORE committing (decompose/enqueue) the PM consults the Critic. The loop
    #     is BOUNDED; on unresolved genuine disagreement it escalates a 🛑 pushback
    #     to the stakeholder and enqueues NO work. With no critic wired this whole
    #     block is skipped — the PM behaves exactly as before (behavior-preserving).
    if critic is not None:
        rounds = critic_rounds if critic_rounds is not None else _critic_rounds()
        plan, outcome, rounds_run, concern_count = _run_consensus(
            conn, task, plan, threshold, sink,
            critic=critic, rounds=max(1, rounds),
            registry=registry, skills=skills, charter=charter, overlay=overlay,
            tid=tid,
        )
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_PM_CONSENSUS,
                task_id=task.id,
                payload={
                    "goal": goal,
                    "rounds": rounds_run,
                    "outcome": outcome,
                    "concern_count": concern_count,
                },
            )
        )
        if outcome == CONSENSUS_ESCALATED:
            reason = (
                "PM↔Critic could not reach consensus after "
                f"{rounds_run} round(s); escalating a genuine disagreement"
            )
            sink.emit(
                make_event(
                    workstream=task.workstream,
                    type=EVENT_PM_PUSHBACK,
                    task_id=task.id,
                    payload={"goal": goal, "confidence": plan.confidence, "reason": reason},
                )
            )
            approval = request_approval(
                conn,
                task_id=task.id,
                role="pm",
                tool="pm.consensus",
                capabilities=[],
                tier=PUSHBACK_TIER,
                reason=reason,
                sink=sink,
                workstream=task.workstream,
            )
            approval_id = str(approval.id) if approval is not None else None
            _traj_step(
                conn, tid, "escalate",
                "PM↔Critic could not reach consensus → raised a 🛑 pushback",
                rationale=reason,
                choice=PUSHBACK_TIER, confidence=plan.confidence,
                refs={"rounds": rounds_run, "concern_count": concern_count,
                      "approval_id": approval_id},
            )
            _traj_close(conn, tid, outcome_summary="escalated: unresolved disagreement")
            return PlanResult(
                goal=goal, decision="pushback", restated_goal=plan.restated_goal,
                confidence=plan.confidence, feasible=True, reason=reason,
                approval_id=approval_id, research_task_id=research_task_id,
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
    # Stamp every created task with this trajectory (ADR-0020 outcome attribution),
    # but only when a trajectory is actually open — omit the kwarg otherwise so a
    # fake enqueue seam that predates it keeps working (behavior-preserving).
    traj_link = {"trajectory_id": tid} if tid is not None else {}
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
            **traj_link,
        )
        id_by_index[i] = str(work.id)
    # Report ids in the PM's original item order (stable, human-readable).
    work_task_ids = [id_by_index[i] for i in range(1, n + 1)]

    _traj_step(
        conn, tid, "decompose",
        f"decomposed the goal into {n} up_for_grabs work item(s)",
        rationale=(
            "Enqueued one up_for_grabs task per work item, each carrying its own "
            "concrete success criterion + marker (the Executor/Verifier contract) "
            "and its dependency edges, and linked back to this trajectory for "
            "outcome attribution."
        ),
        choice="decompose", confidence=plan.confidence,
        refs={"task_ids": work_task_ids, "item_count": n},
    )

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
    _traj_step(
        conn, tid, "commit",
        f"committed the decomposition (emitted pm.planned; {n} work item(s))",
        rationale=(
            f"All {n} work item(s) enqueued and linked to this trajectory; the "
            "decision is now observable and attributable to its outcomes."
        ),
        choice="planned", confidence=plan.confidence,
        refs={"task_ids": work_task_ids},
    )
    _traj_close(conn, tid, outcome_summary=f"planned: decomposed into {n} work item(s)")
    return PlanResult(
        goal=goal, decision="planned", restated_goal=plan.restated_goal,
        confidence=plan.confidence, feasible=True, reason=plan.reason,
        work_item_count=len(work_task_ids), work_task_ids=work_task_ids,
        research_task_id=research_task_id,
    )


# --- Stuck-task re-decomposition (ADR-0023, R2) -----------------------------
#
# The PM's response to the supervisor's ``task.stuck`` SIGNAL: a task that is too
# big / ill-posed to finish as ONE unit is broken into SMALLER subtasks, each
# individually more likely to herd through — NOT the same monolith re-enqueued.
# Coordination is via the queue only (CLAUDE.md invariant 1): the supervisor emits
# ``task.stuck`` + supersedes the attempt (R1); a queue consumer
# (:func:`runtime.scheduler.dispatch_replans`) turns that signal into ONE ``replan``
# task; the worker dispatches it here. The original stays ``abandoned`` (superseded).
#
# BOUNDED (ADR-0023): every subtask carries a ``replan_depth`` in its payload; each
# round stamps ``depth + 1``. Once a stuck task's depth reaches ``PM_MAX_REPLAN_DEPTH``
# the PM STOPS re-decomposing and escalates to a human 🛑 — a subtask that itself
# keeps getting stuck can never recurse forever.


class ReplanResult(BaseModel):
    """What the PM decided on a ``replan`` task (ids/counts only, no bodies).

    ``decision`` is ``"replanned"`` (re-decomposed into ``subtask_ids``) or
    ``"escalated"`` (the replan-depth cap was reached → a human 🛑 approval was
    raised, no subtasks enqueued). ``missing=True`` marks a replan whose stuck task
    row could not be read (nothing to do).
    """

    stuck_task_id: str
    decision: str = REPLAN_DECOMPOSED
    goal: str = ""
    replan_depth: int = 0
    max_depth: int = DEFAULT_MAX_REPLAN_DEPTH
    subtask_count: int = 0
    subtask_ids: list[str] = Field(default_factory=list)
    approval_id: Optional[str] = None
    missing: bool = False


def _stuck_trajectory_id(conn: Any, task_id: UUID) -> Optional[UUID]:
    """Read a task's linked ``trajectory_id`` (a DB column not on the Task model).

    Observe-only + degrade-safe (ADR-0017/0020): the trajectory link is used purely
    for outcome attribution, so any failure (no conn / fake seam / DB blip) returns
    ``None`` and the replan proceeds unlinked — it is NEVER load-bearing.
    """
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trajectory_id FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
        if not getattr(conn, "autocommit", True):
            conn.commit()
        return row["trajectory_id"] if row else None
    except Exception:  # pragma: no cover - defensive: attribution is never load-bearing
        log.warning("replan: could not read trajectory_id for %s; proceeding unlinked", task_id)
        return None


def _replan_goal(stuck: Task) -> str:
    """The goal to re-decompose: the stuck task's own spec (payload), else default."""
    payload = stuck.payload or {}
    for key in ("goal", "objective", "instructions", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_OBJECTIVE


def _smaller_subtasks(goal: str, plan: Plan, stuck_task_id: UUID) -> list[WorkItem]:
    """The SMALLER decomposition to enqueue for a stuck task (always ≥2 items).

    Prefers the model's fresh decomposition of the goal (the dry-run/real plan
    yields 2–3 parts). Defensive floor: if the plan somehow yields fewer than two
    work items, split the goal into a deterministic 2-step sequence (draft →
    finalize, the second depending on the first) so a stuck monolith is NEVER just
    re-enqueued as a single item again.
    """
    items = [it for it in plan.work_items if it is not None]
    if len(items) >= 2:
        return items
    base = goal if len(goal) <= 70 else goal[:67] + "..."
    return [
        WorkItem(
            title=f"Subtask 1/2: draft — {base}",
            type=DEFAULT_WORK_TASK_TYPE,
            instructions=f"Produce the first, smaller half of the goal: {goal}",
            success_criterion=(
                f"The part-1 artifact exists and contains the marker "
                f"'studio-ok:replan:{stuck_task_id}:1'."
            ),
            marker=f"studio-ok:replan:{stuck_task_id}:1",
            depends_on=[],
        ),
        WorkItem(
            title=f"Subtask 2/2: finalize — {base}",
            type=DEFAULT_WORK_TASK_TYPE,
            instructions=f"Complete the remaining, smaller half of the goal: {goal}",
            success_criterion=(
                f"The part-2 artifact exists and contains the marker "
                f"'studio-ok:replan:{stuck_task_id}:2'."
            ),
            marker=f"studio-ok:replan:{stuck_task_id}:2",
            depends_on=[1],
        ),
    ]


def run_pm_replan(
    conn: Any,
    stuck_task_id: UUID,
    sink: Optional[EventSink] = None,
    *,
    registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
    max_depth: Optional[int] = None,
    enqueue: Callable[..., Task] = None,  # type: ignore[assignment]
    call_model: Callable[..., Any] = _call_model,
    request_approval: Callable[..., Any] = _request_approval,
) -> ReplanResult:
    """Re-decompose a superseded (stuck) task into SMALLER subtasks (ADR-0023, R2).

    Reads the PRESERVED abandoned task row (``get_task``) — its ``type`` / ``payload``
    / ``workstream`` / ``depends_on`` / ``trajectory_id`` — and, UNLESS the replan
    depth cap is reached, obtains a fresh plan for its goal and enqueues N (≥2)
    ``up_for_grabs`` subtasks (a dependency DAG when the plan has ordering), each
    stamped to link back to the original (``replan_of`` / ``parent_task_id`` +
    ``replan_depth = depth + 1``) and inheriting the original's ``trajectory_id`` when
    present. Emits a body-free ``task.replanned`` (original id + new subtask ids +
    count). The original stays ``abandoned``.

    BOUNDED: when the stuck task's ``replan_depth`` (payload, default 0) has reached
    ``max_depth`` (arg > ``PM_MAX_REPLAN_DEPTH`` env > default) the PM STOPS and raises
    a 🛑 human approval (the existing approval path) + emits ``task.replan_escalated``,
    enqueuing NO subtasks — so a repeatedly-stuck subtask can never replan forever.

    ``enqueue`` / ``call_model`` / ``request_approval`` are injectable for tests
    (defaults are the real functions). Coordination is queue-only: this is invoked
    by the worker servicing a ``replan`` task, never called agent-to-agent.
    """
    if enqueue is None:  # deferred default to avoid an import cycle at module load
        from ..tasks import enqueue_task
        enqueue = enqueue_task
    from ..tasks import get_task

    sink = sink or NullEventSink()
    cap = max_depth if max_depth is not None else _max_replan_depth()

    stuck = get_task(conn, stuck_task_id)
    if stuck is None:
        log.warning("replan: stuck task %s not found; nothing to re-decompose", stuck_task_id)
        return ReplanResult(stuck_task_id=str(stuck_task_id), missing=True, max_depth=cap)

    payload = stuck.payload or {}
    goal = _replan_goal(stuck)
    depth = int(payload.get("replan_depth", 0) or 0)

    # --- Bound: depth cap reached → escalate to a human 🛑, do NOT re-decompose --
    if depth >= cap:
        reason = (
            f"task repeatedly stuck: replan depth {depth} reached the cap {cap}; "
            "a human must replan / rescope it (no further auto re-decomposition)"
        )
        approval = request_approval(
            conn,
            task_id=stuck_task_id,
            role="pm",
            tool="pm.replan",
            capabilities=[],
            tier=PUSHBACK_TIER,
            reason=reason,
            sink=sink,
            workstream=stuck.workstream,
        )
        approval_id = str(approval.id) if approval is not None else None
        sink.emit(
            make_event(
                workstream=stuck.workstream,
                type=EVENT_TASK_REPLAN_ESCALATED,
                task_id=stuck_task_id,
                payload={"replan_depth": depth, "max_depth": cap, "approval_id": approval_id},
            )
        )
        log.warning(
            "replan of %s escalated to human 🛑 (depth %d >= cap %d)",
            stuck_task_id, depth, cap,
        )
        return ReplanResult(
            stuck_task_id=str(stuck_task_id), decision=REPLAN_ESCALATED, goal=goal,
            replan_depth=depth, max_depth=cap, approval_id=approval_id,
        )

    # --- Re-decompose into SMALLER subtasks ---------------------------------
    # Reuse the PM's plan seam: obtain a fresh plan for the goal, then enqueue its
    # work items (guaranteed ≥2) as a DAG linked back to the original.
    plan = _obtain_plan(
        conn, stuck, goal, sink,
        registry=registry, skills=skills, call_model=call_model,
        charter=charter, overlay=overlay,
    )
    items = _smaller_subtasks(goal, plan, stuck_task_id)
    n = len(items)

    edges: dict[int, list[int]] = {}
    for i, item in enumerate(items, start=1):
        edges[i] = sorted({d for d in item.depends_on if 1 <= d <= n and d != i})
    assert_acyclic(edges)  # DependencyCycle on a cyclic / self-referential plan
    order = _topo_order(edges)  # prerequisites first, so their ids exist on enqueue

    # Inherit the original's trajectory for outcome attribution (ADR-0020) when set.
    traj_id = _stuck_trajectory_id(conn, stuck_task_id)
    traj_link = {"trajectory_id": traj_id} if traj_id else {}
    id_by_index: dict[int, str] = {}
    for i in order:
        item = items[i - 1]
        marker = (item.marker or "").strip() or f"studio-ok:replan:{stuck_task_id}:{i}"
        wtype = item.type if item.type.startswith("work.") else DEFAULT_WORK_TASK_TYPE
        criterion = item.success_criterion or f"The artifact contains the marker {marker!r}."
        work = enqueue(
            conn,
            workstream=stuck.workstream,
            type=wtype,
            payload={
                "goal": item.instructions or goal,
                "criterion": criterion,
                "marker": marker,
                "title": item.title,
                "item_index": i,
                "item_count": n,
                "attempt": 1,
                # Link back to the superseded original + bound the replan recursion.
                "replan_of": str(stuck_task_id),
                "parent_task_id": str(stuck_task_id),
                "replan_depth": depth + 1,
            },
            priority=stuck.priority,
            depends_on=[UUID(id_by_index[d]) for d in edges[i]],
            **traj_link,
        )
        id_by_index[i] = str(work.id)
    subtask_ids = [id_by_index[i] for i in range(1, n + 1)]

    sink.emit(
        make_event(
            workstream=stuck.workstream,
            type=EVENT_TASK_REPLANNED,
            task_id=stuck_task_id,
            payload={
                "subtask_ids": subtask_ids,
                "subtask_count": n,
                "replan_depth": depth + 1,
            },
        )
    )
    log.info(
        "replanned stuck task %s into %d smaller subtask(s) at depth %d: %s",
        stuck_task_id, n, depth + 1, subtask_ids,
    )
    return ReplanResult(
        stuck_task_id=str(stuck_task_id), decision=REPLAN_DECOMPOSED, goal=goal,
        replan_depth=depth + 1, max_depth=cap,
        subtask_count=n, subtask_ids=subtask_ids,
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
