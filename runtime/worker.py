"""The worker — the on-demand driver that runs one task to completion (M3c).

"Spawning an agent" = enqueuing a task (ADR-0009); the worker is what
materializes to service it. It is a **non-LLM driver** (the reasoning lives in the
roles): it claims a task, dispatches by type, heartbeats while the role works, and
turns the Verifier's verdict into the terminal state — enforcing verify→commit
(architecture §4, CLAUDE.md invariant 4): a work task is never ``done`` until the
Verifier passes.

Dispatch (all state changes via the canonical guarded ``transition``, ADR-0015):

- ``pm.tick`` → :func:`runtime.roles.pm.run_pm_tick` (plan + enqueue work), merge.
- ``work.*``  → the unified dev/review loop: submit (``in_progress →
  ready_for_review``) → :func:`runtime.roles.verifier.verify` as the automated
  reviewer; on pass → ``approved → merged``; on fail → ``reviewer_blocked`` then a
  **bounded** retry (``→ in_progress``) or ``→ abandoned``.

All coordination is through the merged M1 queue + event log; all tools go through
the M2 policy gate (inside the roles); all model calls go through M3b
``call_model`` (inside the roles). The worker itself calls no model and touches no
host state except through those seams.

Run it as an on-demand driver::

    python -m runtime.worker

Config (env): ``WORKER_ID``, ``WORKER_SCRATCH_DIR`` (tool root),
``WORKER_IDLE_SLEEP_S`` (poll gap when the queue is empty),
``WORKER_MAX_WORK_ATTEMPTS`` (verify-fail re-enqueues before failing),
``WORKER_RETRO`` (``on_fail`` (default) | ``always`` | ``off`` — when a terminal
work task triggers a learning Retro), ``WORKER_REVIEW`` (``on_risk`` (default) |
``always`` | ``off`` — when a terminal work task triggers the independent
Reviewer/Whistle-blower risk guard).

``WORKER_RETRO`` / ``WORKER_REVIEW`` set the *base* trigger policy. With
``ADAPTIVE_INTENSITY=on`` (default ``off`` → behavior-preserving) the worker
scales those bases per work episode from FACTS (:mod:`runtime.adaptive`): a
workstream with a high recent error rate gets MORE review/retro (escalated toward
``always``), while a clean one whose budget is tight gets less (relaxed toward
``off``) — throttled so a near-exhausted budget never piles on extra work.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Callable, Optional
from uuid import uuid4

import psycopg
from pydantic import BaseModel

from . import adaptive
from .adaptive import IntensityDecision
from .approvals import (
    STATUS_APPROVED,
    STATUS_DENIED,
    get_approval,
)
from .budget import remaining as budget_remaining
from .db import connect
from .enforce import DbEventSink, EventSink, InvokeStatus, NullEventSink, invoke
from .models import Assignee, Task, TaskStatus, make_event
from .policy import PolicyConfig, load_policy
from .roles.checkers import DEFAULT_REGISTRY, CheckerRegistry
from .roles.executor import run_executor
from .roles.pm import run_pm_tick
from .roles.researcher import RESEARCH_TASK_TYPE, run_research as run_research_default
from .roles.retro import RETRO_TASK_TYPE, run_retro as run_retro_default
from .roles.reviewer import REVIEW_TASK_TYPE, run_review as run_review_default
from .roles.verifier import verify as run_verify_default
from .scheduler import PM_TICK_TYPE
from .skills import SkillRegistry
from .workstream import WorkstreamConfig, resolve_workstream_config
from .tasks import (
    block_task,
    claim_task,
    complete_task,
    enqueue_task,
    find_blocked_tasks,
    heartbeat,
    requeue_blocked_task,
    transition,
)
from .tools import CodingTool, FilesystemTool, ShellTool, ToolRegistry

log = logging.getLogger("runtime.worker")

#: Event emitted when the worker re-enqueues a work task after a verify fail.
EVENT_WORK_RETRY = "work.retry"

#: Event emitted when the worker enqueues a retro after a terminal work task.
EVENT_RETRO_TRIGGERED = "retro.triggered"

#: Event emitted when the worker enqueues a review after a terminal work task.
EVENT_REVIEW_TRIGGERED = "review.triggered"

#: Event emitted when a task blocked on a 🔴 approval is re-queued after a grant.
EVENT_APPROVAL_RESUMED = "approval.resumed"

#: Coding-worker dispatch (architecture §14). A "Need Prototype" task routes to the
#: policy-gated `coding` tool, which runs opencode INSIDE the sandbox. Kept distinct
#: from the generic `work.*` dev/review loop so the coding path has no retry loop.
CODE_TASK_TYPES = ("work.code", "prototype")

#: Role the coding dispatch runs as — the Builder (§14: it only knows "Need
#: Prototype"). It is granted `code.run` in the policy; the 🔴 tier still forces a
#: human approval before opencode ever runs.
CODE_WORKER_ROLE = "builder"

DEFAULT_IDLE_SLEEP_S = 5.0
DEFAULT_MAX_WORK_ATTEMPTS = 2

#: When the worker fires a Retro after a work task reaches a terminal state.
#: ``on_fail`` (default) keeps cost down + matches ADR-0003 "more retro when errors";
#: ``always`` retros every episode; ``off`` disables the learning loop's trigger.
RETRO_ON_FAIL = "on_fail"
RETRO_ALWAYS = "always"
RETRO_OFF = "off"
DEFAULT_RETRO_MODE = RETRO_ON_FAIL
_RETRO_MODES = {RETRO_ON_FAIL, RETRO_ALWAYS, RETRO_OFF}

#: When the worker fires a Reviewer/Whistle-blower after a terminal work task.
#: ``on_risk`` (default) runs the guard adaptively — only when the episode looks
#: risky (failed / re-kicked / over budget), matching ADR-0003 "more review when
#: the error rate is high"; ``always`` reviews every episode; ``off`` disables it.
REVIEW_ON_RISK = "on_risk"
REVIEW_ALWAYS = "always"
REVIEW_OFF = "off"
DEFAULT_REVIEW_MODE = REVIEW_ON_RISK
_REVIEW_MODES = {REVIEW_ON_RISK, REVIEW_ALWAYS, REVIEW_OFF}

# Injectable seams — defaults are the real M1/M3b/role functions; tests pass fakes
# so `run_once` drives the full loop with no database (same idiom as supervisor).
Claimer = Callable[..., Optional[Task]]
Heartbeater = Callable[..., Optional[Task]]
Completer = Callable[..., Optional[Task]]
Enqueuer = Callable[..., Task]
Blocker = Callable[..., Optional[Task]]
Transitioner = Callable[..., Optional[Task]]
IntensityResolver = Callable[..., IntensityDecision]


class RunResult(BaseModel):
    """What one :func:`run_once` pass did — for logging/telemetry and tests."""

    task_id: str
    task_type: str
    #: "pm" | "work" | "code" | "retro" | "review" | "research" | "unknown"
    kind: str
    #: "done" (merged) | "failed" (abandoned) | "blocked"
    outcome: str
    detail: str = ""


def build_registry(scratch_dir: str) -> ToolRegistry:
    """Build the worker's tool registry: a filesystem tool confined to
    ``scratch_dir`` plus the (host-refusing) shell tool. The scratch root need not
    pre-exist — the filesystem tool creates parents on write."""
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=scratch_dir))
    reg.register(ShellTool())
    # The coding worker (opencode) dispatch tool. Registered WITHOUT a sandbox by
    # default — like ShellTool it then refuses to run on the host; a real host
    # wires `CodingTool.with_docker_sandbox(...)` (see runtime/coding-worker.md).
    reg.register(CodingTool())
    return reg


def _handle_pm_tick(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    model_registry,
    enqueue: Enqueuer,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_pm: Callable[..., Any],
    skills: Optional[SkillRegistry] = None,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> RunResult:
    plan = run_pm(
        conn, task, sink, registry=model_registry, skills=skills, enqueue=enqueue,
        charter=charter, overlay=overlay,
    )
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=plan.model_dump(), status=TaskStatus.MERGED)
    if plan.decision == "planned":
        detail = f"decomposed into {plan.work_item_count} work item(s): {plan.work_task_ids}"
    elif plan.decision == "pushback":
        detail = f"pushed back (🛑 approval {plan.approval_id}): {plan.reason}"
    else:  # needs_clarification
        detail = f"needs clarification: {plan.reason}"
    return RunResult(
        task_id=str(task.id),
        task_type=task.type,
        kind="pm",
        outcome="done",
        detail=detail,
    )


def _resolve_retro_mode(retro_mode: Optional[str]) -> str:
    """Resolve the retro trigger policy (arg > ``WORKER_RETRO`` env > default)."""
    mode = (retro_mode if retro_mode is not None else os.environ.get("WORKER_RETRO", "")).strip().lower()
    if mode in _RETRO_MODES:
        return mode
    if mode:
        log.warning("invalid WORKER_RETRO=%r; using default %s", mode, DEFAULT_RETRO_MODE)
    return DEFAULT_RETRO_MODE


def _resolve_review_mode(review_mode: Optional[str]) -> str:
    """Resolve the review trigger policy (arg > ``WORKER_REVIEW`` env > default)."""
    mode = (review_mode if review_mode is not None else os.environ.get("WORKER_REVIEW", "")).strip().lower()
    if mode in _REVIEW_MODES:
        return mode
    if mode:
        log.warning("invalid WORKER_REVIEW=%r; using default %s", mode, DEFAULT_REVIEW_MODE)
    return DEFAULT_REVIEW_MODE


def _resolve_intensity_default(
    conn: Any,
    workstream: str,
    *,
    base_review: str,
    base_retro: str,
) -> IntensityDecision:
    """Resolve this episode's effective review/retro modes from FACTS (ADR-0003).

    The worker's default intensity seam. When ``ADAPTIVE_INTENSITY`` is off
    (default) this returns the base modes verbatim and reads NO telemetry/budget —
    so the worker's static behavior is preserved exactly. When on, it reads the
    workstream's remaining budget headroom (:func:`runtime.budget.remaining`) and
    hands it, with the base modes, to :func:`runtime.adaptive.resolve_modes`, which
    escalates on a high recent error rate and throttles on a tight budget.
    """
    cfg = adaptive.AdaptiveConfig.from_env()
    if not cfg.enabled:
        return IntensityDecision(
            review=base_review, retro=base_retro, research=adaptive.RESEARCH_NORMAL,
            error_rate=0.0, budget_fraction=None, activity=0, adaptive=False,
        )
    try:
        headroom = budget_remaining(conn, workstream)
    except Exception:  # budget is advisory here — never let it break the episode
        log.warning("adaptive: budget read failed for %s; treating as uncapped", workstream)
        headroom = None
    return adaptive.resolve_modes(
        conn, workstream,
        base_review=base_review, base_retro=base_retro,
        budget_remaining=headroom, config=cfg,
    )


def _episode_is_risky(task: Task, outcome: str) -> bool:
    """Adaptive trigger for ``on_risk``: did the episode look risky (ADR-0003)?

    Risky == the work failed, was re-kicked by the supervisor (``retries`` > 0), or
    blew its token budget. A clean, first-try ``done`` is NOT risky, so the guard
    stays quiet + cheap on the happy path and intensifies exactly when the error
    rate rises.
    """
    if outcome == "failed":
        return True
    if (getattr(task, "retries", 0) or 0) > 0:
        return True
    budget = getattr(task, "budget_tokens", None)
    spent = getattr(task, "spent_tokens", 0) or 0
    if budget and spent > budget:
        return True
    return False


def _maybe_enqueue_review(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    enqueue: Enqueuer,
    outcome: str,
    exec_result: Any,
    review_mode: str,
) -> None:
    """Enqueue ONE ``review`` task for a terminal work task, per the review policy.

    Bounded by construction: only ``work.*`` terminal states reach here, the review
    task is a distinct type (never ``work.*``/``pm.tick``/``retro``), and a review
    NEVER enqueues another task — so there is no review-of-a-review (or review↔retro)
    loop. ``on_risk`` fires only on a risky episode; ``always`` on every terminal one;
    ``off`` never. The review carries the target's facts (artifact path/marker +
    spend/budget/retries) so the Reviewer can gather evidence; the trigger event
    carries ids/outcome only (no marker/path/secret).
    """
    if review_mode == REVIEW_OFF:
        return
    if review_mode == REVIEW_ON_RISK and not _episode_is_risky(task, outcome):
        return
    review = enqueue(
        conn,
        workstream=task.workstream,
        type=REVIEW_TASK_TYPE,
        payload={
            "target_task_id": str(task.id),
            "target_task_type": task.type,
            "outcome": outcome,
            "artifact_path": getattr(exec_result, "artifact_path", None),
            "marker": getattr(exec_result, "marker", None),
            "spent_tokens": getattr(task, "spent_tokens", 0) or 0,
            "budget_tokens": getattr(task, "budget_tokens", None),
            "retries": getattr(task, "retries", 0) or 0,
        },
        priority=task.priority,
    )
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_REVIEW_TRIGGERED,
            task_id=task.id,
            payload={"review_task_id": str(review.id), "outcome": outcome, "mode": review_mode},
        )
    )


def _maybe_enqueue_retro(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    enqueue: Enqueuer,
    outcome: str,
    detail: str,
    retro_mode: str,
) -> None:
    """Enqueue ONE retro task for a terminal work task, per the retro policy.

    Bounded by construction: only ``work.*`` terminal states reach here, the retro
    task is a distinct type (never ``work.*``/``pm.tick``), and a retro NEVER
    enqueues another task — so there is no retro-of-a-retro loop. ``on_fail`` fires
    only on a failed episode; ``always`` on every terminal episode; ``off`` never.
    """
    if retro_mode == RETRO_OFF:
        return
    if retro_mode == RETRO_ON_FAIL and outcome != "failed":
        return
    retro = enqueue(
        conn,
        workstream=task.workstream,
        type=RETRO_TASK_TYPE,
        payload={
            "target_task_id": str(task.id),
            "target_task_type": task.type,
            "outcome": outcome,
            "reason": detail,
        },
        priority=task.priority,
    )
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_RETRO_TRIGGERED,
            task_id=task.id,
            payload={"retro_task_id": str(retro.id), "outcome": outcome, "mode": retro_mode},
        )
    )


def _handle_work(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    model_registry,
    heartbeat: Heartbeater,
    transition: Transitioner,
    enqueue: Enqueuer,
    block: Blocker,
    worker_id: str,
    max_attempts: int,
    run_exec: Callable[..., Any],
    run_verify: Callable[..., Any],
    skills: Optional[SkillRegistry] = None,
    retro_mode: str = DEFAULT_RETRO_MODE,
    review_mode: str = DEFAULT_REVIEW_MODE,
    charter: Optional[str] = None,
    exec_overlay: Optional[str] = None,
    verify_overlay: Optional[str] = None,
    checkers: CheckerRegistry = DEFAULT_REGISTRY,
) -> RunResult:
    """Drive one ``work.*`` task through the unified dev/review lifecycle.

    The task arrives ``in_progress`` (claim did grab→start). This drives it, in one
    pass, through: Executor → submit (``in_progress → ready_for_review``) → the
    **Verifier as automated reviewer** → pass (``ready_for_review → approved →
    merged``) or fail (``ready_for_review → reviewer_blocked``; retry
    ``→ in_progress`` while attempts remain, else ``→ abandoned``). A 🔴 approval
    pend parks it ``blocked`` (resumed later). Every hop is a guarded transition
    (telemetry + ``task.transition`` event).
    """
    exec_result = None
    verdict = None
    for attempt in range(1, max_attempts + 1):
        # Heartbeat around each phase — liveness while the role does the work.
        heartbeat(conn, task.id, worker_id)
        exec_result = run_exec(
            conn, task, sink, registry=registry, config=config, model_registry=model_registry,
            charter=charter, overlay=exec_overlay,
        )

        # 🔴 human-in-the-loop: the Executor's tool call PENDed on an approval. Park
        # the task `blocked` on that approval and STOP — do NOT review or merge.
        # invoke already emitted approval.requested; resume_approved re-queues it.
        if exec_result.invoke_status == InvokeStatus.PENDING.value:
            block(conn, task.id, approval_id=exec_result.approval_id,
                  reason="awaiting 🔴 approval")
            return RunResult(
                task_id=str(task.id), task_type=task.type, kind="work",
                outcome="blocked",
                detail=f"blocked on approval {exec_result.approval_id}",
            )

        heartbeat(conn, task.id, worker_id)
        # Submit the produced artifact for review.
        transition(conn, task.id, TaskStatus.READY_FOR_REVIEW,
                   agent_id=worker_id, agent_type="executor",
                   result={"artifact_path": exec_result.artifact_path, "attempt": attempt})

        # The Verifier is the automated reviewer (evidence-over-claims, ADR-0014).
        verdict = run_verify(
            conn, task, exec_result, sink,
            registry=registry, config=config, model_registry=model_registry,
            skills=skills, charter=charter, overlay=verify_overlay, checkers=checkers,
        )
        heartbeat(conn, task.id, worker_id)

        if verdict.passed:
            # review pass → approved → merged (verify→commit: only now is it merged).
            transition(conn, task.id, TaskStatus.APPROVED,
                       agent_id=worker_id, agent_type="verifier",
                       result={"verified": True, "reason": verdict.reason})
            finished = transition(
                conn, task.id, TaskStatus.MERGED,
                agent_id=worker_id, agent_type="verifier",
                result={"verified": True, "reason": verdict.reason,
                        "artifact_path": exec_result.artifact_path},
            ) or task
            _maybe_enqueue_retro(
                conn, task, sink, enqueue=enqueue,
                outcome="done", detail=verdict.reason, retro_mode=retro_mode,
            )
            _maybe_enqueue_review(
                conn, finished, sink, enqueue=enqueue,
                outcome="done", exec_result=exec_result, review_mode=review_mode,
            )
            return RunResult(
                task_id=str(task.id), task_type=task.type, kind="work",
                outcome="done", detail=verdict.reason,
            )

        # review fail → reviewer_blocked. Retry (→ in_progress) if attempts remain.
        transition(conn, task.id, TaskStatus.REVIEWER_BLOCKED,
                   agent_id=worker_id, agent_type="verifier",
                   result={"verified": False, "reason": verdict.reason, "attempt": attempt})
        if attempt < max_attempts:
            transition(conn, task.id, TaskStatus.IN_PROGRESS,
                       agent_id=worker_id, agent_type="executor")
            sink.emit(
                make_event(
                    workstream=task.workstream,
                    type=EVENT_WORK_RETRY,
                    task_id=task.id,
                    payload={"attempt": attempt, "reason": verdict.reason},
                )
            )
            continue

        # Exhausted → abandoned.
        detail = f"verify failed after {attempt} attempt(s): {verdict.reason}"
        finished = transition(
            conn, task.id, TaskStatus.ABANDONED,
            agent_id=worker_id, agent_type="verifier",
            result={"verified": False, "reason": verdict.reason, "attempt": attempt},
        ) or task
        _maybe_enqueue_retro(
            conn, task, sink, enqueue=enqueue,
            outcome="failed", detail=detail, retro_mode=retro_mode,
        )
        _maybe_enqueue_review(
            conn, finished, sink, enqueue=enqueue,
            outcome="failed", exec_result=exec_result, review_mode=review_mode,
        )
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="work",
            outcome="failed", detail=detail,
        )


def _handle_retro(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    model_registry,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_retro: Callable[..., Any],
) -> RunResult:
    """Dispatch a ``retro`` task: distill + store lessons, then commit.

    A retro NEVER enqueues another task (no ``enqueue`` seam is threaded here), so
    the learning loop cannot recurse into a retro-of-a-retro.
    """
    heartbeat(conn, task.id, worker_id)
    result = run_retro(conn, task, sink, model_registry=model_registry)
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=result.model_dump(), status=TaskStatus.MERGED)
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="retro",
        outcome="done", detail=f"distilled {result.lessons_count} lesson(s)",
    )


def _handle_research(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    model_registry,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_research: Callable[..., Any],
) -> RunResult:
    """Dispatch a ``research`` task: search → distill into Knowledge lessons, commit.

    The Researcher gathers via the policy-gated cached search gateway and distills
    the findings into recallable lessons (+ an optional ``reviewed: false``
    candidate skill, off by default). It NEVER enqueues another task (no
    ``enqueue`` seam is threaded here), so there is no research-of-a-research loop.
    """
    heartbeat(conn, task.id, worker_id)
    result = run_research(
        conn, task, sink,
        model_registry=model_registry, tool_registry=registry, policy=config,
    )
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=result.model_dump(), status=TaskStatus.MERGED)
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="research",
        outcome="done",
        detail=f"gathered {result.results_count} source(s), distilled {result.lessons_count} lesson(s)",
    )


def _handle_review(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    model_registry,
    heartbeat: Heartbeater,
    complete: Completer,
    worker_id: str,
    run_review: Callable[..., Any],
    skills: Optional[SkillRegistry] = None,
) -> RunResult:
    """Dispatch a ``review`` task: the independent Reviewer/Whistle-blower guard.

    Reads the target episode's trail + artifact (evidence), assesses risk from
    facts, emits ``review.passed`` / ``review.flagged`` (+ 🚨/🛑 on HIGH), then
    commits the review task. A review NEVER enqueues another task (no ``enqueue``
    seam is threaded here), so it cannot recurse into a review-of-a-review or
    trigger a retro — there is no loop.
    """
    heartbeat(conn, task.id, worker_id)
    result = run_review(
        conn, task, sink,
        registry=registry, config=config, model_registry=model_registry, skills=skills,
    )
    heartbeat(conn, task.id, worker_id)
    complete(conn, task.id, result=result.model_dump(), status=TaskStatus.MERGED)
    detail = (
        f"clean (severity=none)" if result.ok
        else f"flagged severity={result.severity}, {len(result.reasons)} signal(s)"
        + (" 🚨🛑 escalated" if result.severity == "high" else "")
    )
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="review",
        outcome="done", detail=detail,
    )


def _handle_code(
    conn: Any,
    task: Task,
    sink: EventSink,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    heartbeat: Heartbeater,
    complete: Completer,
    block: Blocker,
    worker_id: str,
    invoke_tool: Callable[..., Any] = invoke,
) -> RunResult:
    """Dispatch a "Need Prototype" coding task to the coding worker (architecture §14).

    A single, loop-free pass: the Builder invokes the policy-gated ``coding`` tool,
    which runs opencode **inside the sandbox** (never the host). Because
    ``code.run`` is 🔴, the very first :func:`invoke` returns NEEDS_APPROVAL and the
    task is parked ``blocked`` on that approval — :func:`resume_approved` re-queues
    it once a human grants it, and on that retry ``invoke`` finds the grant and the
    coding worker actually runs (one grant = one run). No verify/retry loop lives
    here: the worker's own exit status is the pass/fail signal.
    """
    heartbeat(conn, task.id, worker_id)
    payload = task.payload or {}
    goal = payload.get("goal", "")
    workspace = payload.get("workspace")

    # Policy-gated dispatch. conn opts into the persisted approval loop (find grant
    # / pend). The tool NEVER runs on the host — it refuses without a sandbox.
    result = invoke_tool(
        role=CODE_WORKER_ROLE,
        tool_name="coding",
        registry=registry,
        config=config,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        goal=goal,
        workspace=workspace,
    )
    heartbeat(conn, task.id, worker_id)

    # 🔴 human-in-the-loop: the dispatch PENDed on an approval. Park the task
    # `blocked` on it and STOP; resume_approved re-queues it once granted.
    if result.status is InvokeStatus.PENDING:
        block(conn, task.id, approval_id=result.approval_id, reason="awaiting 🔴 approval")
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="code",
            outcome="blocked", detail=f"blocked on approval {result.approval_id}",
        )

    if result.status is InvokeStatus.DENIED:
        complete(
            conn, task.id,
            result={"denied": True, "reason": result.decision.reason},
            status=TaskStatus.ABANDONED,
        )
        return RunResult(
            task_id=str(task.id), task_type=task.type, kind="code",
            outcome="failed", detail=f"coding dispatch denied: {result.decision.reason}",
        )

    # EXECUTED (a human grant authorized it): the coding worker ran in the sandbox.
    tool_result = result.result
    worker_ok = bool(tool_result and tool_result.ok)
    output = tool_result.output if tool_result else None
    complete(
        conn, task.id,
        result={
            "worker_ok": worker_ok,
            "worker_cmd": (tool_result.metadata.get("worker_cmd") if tool_result else None),
            "produced_files": (output or {}).get("produced_files") if isinstance(output, dict) else None,
            "exit_code": (output or {}).get("exit_code") if isinstance(output, dict) else None,
        },
        status=TaskStatus.MERGED if worker_ok else TaskStatus.ABANDONED,
    )
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="code",
        outcome="done" if worker_ok else "failed",
        detail=("coding worker succeeded" if worker_ok else "coding worker failed"),
    )


def run_once(
    conn: Any,
    worker_id: str,
    sink: Optional[EventSink] = None,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig] = None,
    model_registry=None,
    skills: Optional[SkillRegistry] = None,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_WORK_ATTEMPTS,
    retro_mode: Optional[str] = None,
    review_mode: Optional[str] = None,
    claim: Claimer = claim_task,
    heartbeat: Heartbeater = heartbeat,
    complete: Completer = complete_task,
    transition: Transitioner = transition,
    enqueue: Enqueuer = enqueue_task,
    block: Blocker = block_task,
    run_pm: Callable[..., Any] = run_pm_tick,
    run_exec: Callable[..., Any] = run_executor,
    run_verify: Callable[..., Any] = run_verify_default,
    run_retro: Callable[..., Any] = run_retro_default,
    run_review: Callable[..., Any] = run_review_default,
    run_research: Callable[..., Any] = run_research_default,
    resolve_config: Callable[[Optional[str]], Optional[WorkstreamConfig]] = resolve_workstream_config,
    resolve_intensity: IntensityResolver = _resolve_intensity_default,
) -> Optional[RunResult]:
    """Claim one task and drive it to a terminal state (the single testable unit).

    Returns ``None`` when nothing is claimable (the caller sleeps), else a
    :class:`RunResult`. Dispatch is by ``task.type``: ``pm.tick`` plans + enqueues
    work; ``work.*`` runs Executor + Verifier and commits/fails per the verdict.
    Every seam (claim/heartbeat/complete/enqueue + the role handlers +
    ``resolve_config``) is injectable so the whole loop runs with no database in
    tests.

    The claimed task's **workstream config** (``resolve_config`` →
    :func:`runtime.workstream.resolve_workstream_config`) makes a vertical
    config-not-code: when present it supplies the role charter + per-role overlays,
    the Verifier's domain checkers, and — merged over the base — this workstream's
    policy grants + skill set. A workstream with no config file falls back to the
    inline base behavior unchanged (behavior-preserving).
    """
    sink = sink or NullEventSink()
    resolved_retro_mode = _resolve_retro_mode(retro_mode)
    resolved_review_mode = _resolve_review_mode(review_mode)
    task = claim(conn, worker_id=worker_id, assignee=assignee, workstream=workstream)
    if task is None:
        return None

    # Resolve the claimed task's workstream config (config-not-code). None when the
    # workstream has no config file → the platform's inline base behavior is used
    # (behavior-preserving). When present, it drives the role prompts (charter +
    # per-role overlays), the Verifier's domain checkers, and — merged over the
    # base — this workstream's policy grants + skill set. Budget/policy are already
    # keyed by workstream in the DB (budgets table / effective policy).
    wcfg = resolve_config(task.workstream)
    eff_config = wcfg.effective_policy(config) if (wcfg and config is not None) else config
    eff_skills = wcfg.effective_skills(skills) if wcfg else skills

    if task.type == PM_TICK_TYPE:
        return _handle_pm_tick(
            conn, task, sink,
            model_registry=model_registry, enqueue=enqueue,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id, run_pm=run_pm,
            skills=eff_skills,
            charter=(wcfg.charter if wcfg else None),
            overlay=(wcfg.overlay_for("pm") if wcfg else None),
        )

    if task.type == RETRO_TASK_TYPE:
        # A retro distills lessons from a finished episode. It never enqueues
        # another task, so pm.tick / retro never trigger a retro (no loop).
        return _handle_retro(
            conn, task, sink,
            model_registry=model_registry,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id,
            run_retro=run_retro,
        )

    if task.type == RESEARCH_TASK_TYPE:
        # The Researcher mines external best-practice into Knowledge lessons via the
        # policy-gated cached search gateway. It enqueues NOTHING (no enqueue seam
        # threaded), so a research task cannot spawn another — no research-loop.
        return _handle_research(
            conn, task, sink,
            registry=registry, config=eff_config, model_registry=model_registry,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id,
            run_research=run_research,
        )

    if task.type == REVIEW_TASK_TYPE:
        # The independent Reviewer/Whistle-blower guard over a finished episode. It
        # never enqueues another task (no enqueue seam threaded), so a review can
        # trigger neither another review nor a retro (no loop).
        return _handle_review(
            conn, task, sink,
            registry=registry, config=eff_config, model_registry=model_registry,
            heartbeat=heartbeat, complete=complete, worker_id=worker_id,
            run_review=run_review, skills=eff_skills,
        )

    if task.type in CODE_TASK_TYPES:
        # "Need Prototype" → the coding worker (opencode) dispatch, run inside the
        # sandbox via the policy-gated `coding` tool. Checked BEFORE the generic
        # `work.*` branch so `work.code` takes the loop-free coding path (§14).
        return _handle_code(
            conn, task, sink,
            registry=registry, config=eff_config,
            heartbeat=heartbeat, complete=complete, block=block,
            worker_id=worker_id,
        )

    if task.type.startswith("work."):
        # Adaptive orchestration intensity (ADR-0003): scale the base review/retro
        # modes by this workstream's recent error rate + budget headroom. Off by
        # default → the base (static) modes pass through unchanged (and no
        # telemetry/budget is read), so behavior is preserved.
        decision = resolve_intensity(
            conn, task.workstream,
            base_review=resolved_review_mode, base_retro=resolved_retro_mode,
        )
        if decision.adaptive and (
            decision.review != resolved_review_mode or decision.retro != resolved_retro_mode
        ):
            log.info(
                "adaptive intensity ws=%s error_rate=%.2f budget_frac=%s activity=%d: "
                "review %s->%s retro %s->%s",
                task.workstream, decision.error_rate,
                ("%.2f" % decision.budget_fraction) if decision.budget_fraction is not None else "n/a",
                decision.activity,
                resolved_review_mode, decision.review,
                resolved_retro_mode, decision.retro,
            )
        return _handle_work(
            conn, task, sink,
            registry=registry, config=eff_config, model_registry=model_registry,
            heartbeat=heartbeat, transition=transition, enqueue=enqueue, block=block,
            worker_id=worker_id, max_attempts=max_attempts,
            run_exec=run_exec, run_verify=run_verify, skills=eff_skills,
            retro_mode=decision.retro, review_mode=decision.review,
            charter=(wcfg.charter if wcfg else None),
            exec_overlay=(wcfg.overlay_for("executor") if wcfg else None),
            verify_overlay=(wcfg.overlay_for("verifier") if wcfg else None),
            checkers=(wcfg.checker_registry() if wcfg else DEFAULT_REGISTRY),
        )

    # Unknown task type — fail it explicitly rather than silently dropping it.
    complete(
        conn, task.id,
        result={"error": f"no handler for task type {task.type!r}"},
        status=TaskStatus.ABANDONED,
    )
    return RunResult(
        task_id=str(task.id), task_type=task.type, kind="unknown",
        outcome="failed", detail="no handler for task type",
    )


class ResumeResult(BaseModel):
    """What one :func:`resume_approved` pass did — task ids advanced, for logging."""

    #: Tasks re-queued because their approval was granted (will retry + execute).
    resumed: list[str] = []
    #: Tasks failed because their approval was denied (the 🔴 action was refused).
    failed: list[str] = []


def resume_approved(
    conn: Any,
    sink: Optional[EventSink] = None,
    *,
    complete: Completer = complete_task,
    requeue: Blocker = requeue_blocked_task,
) -> ResumeResult:
    """Advance tasks parked ``blocked`` on an approval a human has now resolved.

    One bounded pass over the currently-blocked tasks (no loops):

    - **approved** (a live grant) → re-queue the task so a worker retries; on that
      retry :func:`runtime.enforce.invoke` finds the grant and executes (then
      consumes it). Emits ``approval.resumed``.
    - **denied** → fail the task (``complete_task`` ``failed``, force): the 🔴
      action was refused, so the work cannot proceed.
    - **still pending / missing** → left untouched for a later pass.

    Call it from the worker/supervisor loop (like the stale-task sweep). It never
    executes a tool itself — it only moves task state; the actual 🔴 execution
    happens on the worker's normal retry through the enforced ``invoke`` path.
    """
    sink = sink or NullEventSink()
    result = ResumeResult()
    for task in find_blocked_tasks(conn):
        approval_id = (task.result or {}).get("blocked_on_approval")
        if not approval_id:
            continue
        approval = get_approval(conn, approval_id)
        if approval is None:
            continue

        if approval.status == STATUS_APPROVED:
            if requeue(conn, task.id) is not None:
                sink.emit(
                    make_event(
                        workstream=task.workstream,
                        type=EVENT_APPROVAL_RESUMED,
                        task_id=task.id,
                        payload={"approval_id": approval_id, "status": "queued"},
                    )
                )
                result.resumed.append(str(task.id))
        elif approval.status == STATUS_DENIED:
            failed = complete(
                conn, task.id,
                result={"approved": False, "reason": approval.reason,
                        "blocked_on_approval": approval_id},
                status=TaskStatus.ABANDONED, force=True,
            )
            if failed is not None:
                result.failed.append(str(task.id))
    return result


# --- Config + loop ----------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def default_scratch_dir() -> str:
    return os.environ.get(
        "WORKER_SCRATCH_DIR",
        os.path.join(tempfile.gettempdir(), "ai_studio_scratch"),
    )


def run(
    worker_id: Optional[str] = None,
    *,
    idle_sleep_s: Optional[float] = None,
    assignee: Optional[Assignee] = None,
    workstream: Optional[str] = None,
) -> None:
    """Loop: claim + service tasks; sleep ``idle_sleep_s`` only when the queue is
    empty. Reconnects on a dropped connection and never lets one bad task kill the
    driver. Runs keyless — ``call_model`` falls back to dry-run with no keys."""
    worker_id = worker_id or os.environ.get("WORKER_ID") or f"worker-{uuid4().hex[:8]}"
    idle_sleep_s = (
        _env_float("WORKER_IDLE_SLEEP_S", DEFAULT_IDLE_SLEEP_S)
        if idle_sleep_s is None
        else idle_sleep_s
    )
    max_attempts = _env_int("WORKER_MAX_WORK_ATTEMPTS", DEFAULT_MAX_WORK_ATTEMPTS)
    registry = build_registry(default_scratch_dir())
    config = load_policy()
    # Discover skills once at startup; the PM composes relevant, reviewed ones
    # into its plan prompt on demand (ADR-0008). Empty registry if none exist.
    skills = SkillRegistry.discover()
    log.info("worker %s: %d skill(s) available", worker_id, len(skills))

    log.info("worker %s starting (scratch=%s)", worker_id, default_scratch_dir())
    conn: Optional[psycopg.Connection] = None
    while True:
        try:
            if conn is None or conn.closed:
                conn = connect()
            # Advance any task whose 🔴 approval a human has resolved since last pass
            # (grant → re-queue for retry+execute; deny → fail). Bounded, one pass.
            resume_approved(conn, DbEventSink(conn))
            result = run_once(
                conn, worker_id, DbEventSink(conn),
                registry=registry, config=config, skills=skills,
                assignee=assignee, workstream=workstream, max_attempts=max_attempts,
            )
            if result is None:
                time.sleep(idle_sleep_s)
            else:
                log.info(
                    "worker %s: %s %s -> %s (%s)",
                    worker_id, result.kind, result.task_id, result.outcome, result.detail,
                )
        except Exception:
            log.exception("worker pass failed; will retry after idle sleep")
            conn = _safe_close(conn)
            time.sleep(idle_sleep_s)


def _safe_close(conn: Optional[psycopg.Connection]) -> None:
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    return None


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
