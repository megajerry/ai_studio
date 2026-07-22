"""PM role — plan + confidence gate, then enqueue ONE work task (M3c).

The PM owns *completion + the confidence gate* (architecture §3) and is the only
role that plans. On a ``pm.tick`` pulse it:

1. resolves a **goal** (task payload → objective string → default);
2. runs a lightweight **confidence gate** — restates the goal and fixes a single,
   checkable **success criterion**, using a ``call_model(role="pm",
   task_type="plan", quality="high", …)`` dry-run call (routed + costed +
   logged like any model call);
3. **enqueues one work task** (``work.demo``) carrying the goal + criterion, i.e.
   "spawns" the Executor by enqueuing a task (ADR-0009) — it never does the work
   itself;
4. emits a ``pm.planned`` event.

The PM touches the host through nothing but ``call_model`` and the task queue —
no direct tool call, no agent-to-agent call (CLAUDE.md invariants 1 & 2).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel

from ..enforce import EventSink, NullEventSink
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..tasks import enqueue_task

#: Role event: the PM committed to a goal + success criterion and enqueued work.
EVENT_PM_PLANNED = "pm.planned"

#: The work-task type the PM enqueues (Executor + Verifier service ``work.*``).
WORK_TASK_TYPE = "work.demo"

#: Default objective when a pulse carries no explicit goal.
DEFAULT_OBJECTIVE = "Prove the studio operates end-to-end in dry-run."

# Inline prompt template (a real Agent Skills layer is a later milestone, ADR-0008).
_PLAN_PROMPT = (
    "You are the studio PM. Restate the goal in one sentence and define ONE "
    "concrete, independently checkable success criterion for it. Goal: {goal}"
)


class PlanResult(BaseModel):
    """What the PM decided on a tick (returned to the worker for the task result)."""

    goal: str
    criterion: str
    #: The token the work artifact must contain for the Verifier to pass.
    marker: str
    #: id of the enqueued work task (str for JSON-friendliness in the result).
    work_task_id: Optional[str] = None


def _resolve_goal(task: Task) -> str:
    payload = task.payload or {}
    for key in ("goal", "objective"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_OBJECTIVE


def run_pm_tick(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    registry: Optional[Registry] = None,
    enqueue: Callable[..., Task] = enqueue_task,
) -> PlanResult:
    """Service one ``pm.tick`` task: confidence-gate a goal and enqueue work.

    ``conn`` is passed to :func:`enqueue` (real DB) and to ``call_model`` for
    token accounting; a test may inject a fake ``enqueue`` and pass a fake conn.
    The confidence-gate model call is a dry-run (keyless) call — its text is not
    parsed; the criterion is fixed deterministically so the Verifier has an
    unambiguous, checkable target.
    """
    sink = sink or NullEventSink()
    goal = _resolve_goal(task)

    # 1. Confidence gate — restate the goal + define a criterion via a model call.
    call_model(
        role="pm",
        task_type="plan",
        messages=[{"role": "user", "content": _PLAN_PROMPT.format(goal=goal)}],
        quality="high",
        registry=registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # A deterministic, independently checkable success criterion + marker. The
    # Executor must produce an artifact containing `marker`; the Verifier checks
    # exactly that — no dependence on the (dry-run) model's free text.
    marker = f"studio-ok:{task.id}"
    criterion = f"A scratch artifact exists and contains the marker {marker!r}."

    # 2. Enqueue ONE work task (the PM spawns the Executor via the queue, ADR-0009).
    work = enqueue(
        conn,
        workstream=task.workstream,
        type=WORK_TASK_TYPE,
        payload={
            "goal": goal,
            "criterion": criterion,
            "marker": marker,
            "attempt": 1,
        },
        priority=task.priority,
    )

    # 3. Emit pm.planned — the plan is now traceable in the event log.
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_PM_PLANNED,
            task_id=task.id,
            payload={
                "goal": goal,
                "criterion": criterion,
                "work_task_id": str(work.id),
                "work_task_type": WORK_TASK_TYPE,
            },
        )
    )

    return PlanResult(
        goal=goal,
        criterion=criterion,
        marker=marker,
        work_task_id=str(work.id),
    )
