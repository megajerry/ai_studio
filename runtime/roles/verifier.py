"""Verifier role — the INDEPENDENT verify→commit gate (M3c).

Architecture §4 / CLAUDE.md invariant 4: no work is committed as ``done`` until an
*independent* check confirms the success criterion. The Verifier is that check.
It is deliberately **read-only** (policy role ``verifier`` grants only ``fs.read``)
so it can never "fix" the work it is judging — it only inspects.

It runs two things:

- a **deterministic check** — re-read the artifact the Executor produced (via the
  policy-gated ``invoke(role="verifier", tool_name="filesystem", op="read", …)``)
  and confirm it contains the goal's marker. This is the gate's decision.
- a **model judgement** — a ``call_model(role="verifier", task_type="verify", …)``
  dry-run call, logged for traceability (the dry-run model cannot truly judge, so
  the deterministic check decides pass/fail).

The worker turns a pass into ``complete_task(status=done)`` and a fail into a
bounded re-enqueue or ``complete_task(status=failed)`` — a task is never ``done``
until :func:`verify` returns ``passed``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..tools import ToolRegistry
from .executor import ExecutorResult

#: Role events for the verify→commit decision.
EVENT_VERIFY_PASSED = "verify.passed"
EVENT_VERIFY_FAILED = "verify.failed"

_VERIFY_PROMPT = (
    "You are the studio Verifier. Independently judge whether the work meets the "
    "criterion. Criterion: {criterion}. Respond pass or fail with one reason."
)


class VerifyResult(BaseModel):
    """The gate's verdict."""

    passed: bool
    reason: str


def verify(
    conn: Any,
    task: Task,
    result: ExecutorResult,
    sink: Optional[EventSink] = None,
    *,
    registry: ToolRegistry,
    config: Optional[PolicyConfig] = None,
    model_registry: Optional[Registry] = None,
) -> VerifyResult:
    """Independently verify ``result`` against ``task``'s success criterion.

    Returns a :class:`VerifyResult`; the worker acts on it (commit vs re-enqueue).
    ``registry`` must contain the same ``filesystem`` tool (same root) the
    Executor wrote to, so the Verifier reads the real artifact.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    criterion = payload.get("criterion", "")
    marker = (payload.get("marker") or getattr(result, "marker", None) or "")

    # Model judgement (dry-run, keyless) — logged for traceability.
    call_model(
        role="verifier",
        task_type="verify",
        messages=[{"role": "user", "content": _VERIFY_PROMPT.format(criterion=criterion)}],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    verdict = _check(task, result, marker, sink, registry, config)

    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_VERIFY_PASSED if verdict.passed else EVENT_VERIFY_FAILED,
            task_id=task.id,
            payload={"passed": verdict.passed, "reason": verdict.reason},
        )
    )
    return verdict


def _check(
    task: Task,
    result: ExecutorResult,
    marker: str,
    sink: EventSink,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
) -> VerifyResult:
    """Deterministic gate: re-read the artifact and confirm the marker is present."""
    artifact_path = getattr(result, "artifact_path", None)
    if not artifact_path:
        return VerifyResult(passed=False, reason="no artifact produced by executor")
    if not marker:
        return VerifyResult(passed=False, reason="no success marker defined")

    read = invoke(
        role="verifier",
        tool_name="filesystem",
        registry=registry,
        config=config,
        events=sink,
        workstream=task.workstream,
        task_id=task.id,
        op="read",
        path=artifact_path,
    )
    if read.status is not InvokeStatus.EXECUTED or not (read.result and read.result.ok):
        return VerifyResult(
            passed=False, reason=f"could not read artifact ({read.status.value})"
        )

    content = read.result.output or ""
    if marker in content:
        return VerifyResult(passed=True, reason=f"artifact contains marker {marker!r}")
    return VerifyResult(passed=False, reason=f"marker {marker!r} not found in artifact")
