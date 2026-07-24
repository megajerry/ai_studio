"""Executor role — DO the work task via a tool + a model (M3c).

The Executor services a ``work.*`` task. It is the ``Executor`` box of the
verify→commit chain (architecture §4): it produces a result but does NOT decide
whether the task is done — the Verifier does that independently.

It acts through exactly the two sanctioned seams:

- a **policy-gated tool call** — ``invoke(role="executor", tool_name="filesystem",
  op="write", …)`` into the confined scratch root (🟡 fs.write, auto-allowed +
  logged). It never calls the tool's ``execute`` directly;
- a **model call** — ``call_model(role="executor", task_type="execute", …)`` (a
  dry-run, keyless completion), routed + costed + logged.

Heartbeats are the worker's responsibility (:mod:`runtime.worker`), not the
role's.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..event_types import EVENT_EXECUTOR_ACTED
from ..policy import PolicyConfig
from ..skills import SkillRegistry, emit_skill_applied
from ..tools import ToolRegistry
from .lessons import recall_lesson_texts
from .prompt import compose_role_prompt

#: Role event (``executor.acted``): the Executor finished producing a result for
#: a work task. Imported from the canonical :mod:`runtime.event_types`.

_EXEC_PROMPT = (
    "You are the studio Executor. Carry out this task and describe what you "
    "produced in one line. Goal: {goal}. Success criterion: {criterion}."
)

#: Selection query for the Executor's skills (on-demand injection, ADR-0008).
_EXEC_SKILL_QUERY = "execute build implement produce artifact do the work"


class ExecutorResult(BaseModel):
    """The Executor's output, handed to the Verifier and to the worker."""

    ok: bool
    #: Path (relative to the tool root) of the artifact written, if any.
    artifact_path: Optional[str] = None
    #: The marker the artifact was written to contain (the Verifier's target).
    marker: Optional[str] = None
    #: Outcome of the policy-gated tool call ("executed" | "denied" | "pending").
    invoke_status: str = InvokeStatus.DENIED.value
    #: Set when the tool call PENDs on a 🔴 approval — the worker parks the task
    #: `blocked` on this approval id until it is resolved (runtime/approvals.py).
    approval_id: Optional[UUID] = None
    note: str = ""


def run_executor(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig] = None,
    model_registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> ExecutorResult:
    """Do ``task`` and return an :class:`ExecutorResult` (does not commit it).

    ``registry`` is the *tool* registry (must contain a ``filesystem`` tool bound
    to the scratch root); ``model_registry`` is the optional model catalog for
    ``call_model``. Both are injected so the role is testable with a temp dir and
    a keyless dry-run model. ``skills`` (optional) supplies on-demand skills and
    ``charter``/``overlay`` the vertical's config-driven prompt framing (all
    default absent → behavior-preserving).
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    goal = payload.get("goal", "")
    criterion = payload.get("criterion", "")
    marker = payload.get("marker") or f"studio-ok:{task.id}"

    # 1. Model call (dry-run, keyless) — the "execution" reasoning step. The prompt
    #    is assembled through the shared role assembler: Executor persona + charter/
    #    overlay (vertical config) + relevant reviewed skills (ADR-0008) + any
    #    durable lessons prior retros distilled for this workstream (ADR-0003),
    #    auto-injected before the role acts.
    lessons = recall_lesson_texts(conn, task.workstream, f"{goal} {criterion}")
    selected = skills.select(_EXEC_SKILL_QUERY) if skills is not None else None
    prompt = compose_role_prompt(
        _EXEC_PROMPT.format(goal=goal, criterion=criterion),
        workstream_charter=charter,
        role_overlay=overlay,
        skills=selected,
        lessons=lessons,
        budget_aware=True,
    )
    # P0 attribution (ADR-0024): body-free skill.applied for the injected skill(s).
    emit_skill_applied(sink, task_id=task.id, role="executor",
                       workstream=task.workstream, skills=selected)
    completion = call_model(
        role="executor",
        task_type="execute",
        messages=[{"role": "user", "content": prompt}],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # 2. Policy-gated tool call — write the artifact into the confined scratch root.
    artifact_path = f"work-{task.id}.txt"
    content = f"{marker}\ngoal: {goal}\nexecutor-note: {completion.text}\n"
    result = invoke(
        role="executor",
        tool_name="filesystem",
        registry=registry,
        config=config,
        events=sink,
        conn=conn,  # opt into the persisted approval loop (find grant / pend)
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=artifact_path,
        content=content,
    )

    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and result.result.ok
    )
    exec_result = ExecutorResult(
        ok=wrote,
        artifact_path=artifact_path if wrote else None,
        marker=marker,
        invoke_status=result.status.value,
        approval_id=result.approval_id if result.status is InvokeStatus.PENDING else None,
        note=("wrote artifact" if wrote else f"tool call not executed: {result.status.value}"),
    )

    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_EXECUTOR_ACTED,
            task_id=task.id,
            payload={
                "ok": exec_result.ok,
                "invoke_status": exec_result.invoke_status,
                "artifact_path": exec_result.artifact_path,
            },
        )
    )
    return exec_result
