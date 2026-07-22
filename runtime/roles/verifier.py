"""Verifier role — the INDEPENDENT verify→commit gate (M3c).

Architecture §4 / CLAUDE.md invariant 4: no work is committed as ``done`` until an
*independent* check confirms the success criterion. The Verifier is that check.
It is deliberately **read-only** (policy role ``verifier`` grants only ``fs.read``)
so it can never "fix" the work it is judging — it only inspects.

**Evidence over claims (ADR-0014).** The Verifier is a *validator*, so it judges
against evidence it observes itself — it re-reads the ACTUAL artifact and checks
the success criterion against its real contents. It never trusts the Executor's
assertion of success (``ExecutorResult.ok``); a "done" claim with an artifact that
does not satisfy the criterion still FAILS. The ``rigorous-review`` skill encodes
this doctrine and is injected into the Verifier's prompt when a skill registry is
supplied (mirroring how the PM composes its skill).

It runs two things:

- a **deterministic evidence check** — re-read the artifact the Executor produced
  (via the policy-gated ``invoke(role="verifier", tool_name="filesystem",
  op="read", …)``) and confirm its real contents contain the goal's marker (the
  success criterion). This observed evidence — not any claim — is the gate's
  decision.
- a **model judgement** — a ``call_model(role="verifier", task_type="verify", …)``
  dry-run call, logged for traceability (the dry-run model cannot truly judge, so
  the deterministic evidence check decides pass/fail).

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
from ..skills import SkillRegistry, compose_prompt
from ..tools import ToolRegistry
from .executor import ExecutorResult

#: Role events for the verify→commit decision.
EVENT_VERIFY_PASSED = "verify.passed"
EVENT_VERIFY_FAILED = "verify.failed"

# Base persona prompt. On-demand skills (ADR-0008) — notably `rigorous-review`
# (ADR-0014, the evidence-over-claims doctrine) — are composed on top when a
# SkillRegistry is supplied; see `_compose_verify_prompt`.
_VERIFY_PROMPT = (
    "You are the studio Verifier, a validator. Independently judge whether the "
    "work meets the criterion using EVIDENCE you observe yourself — read the "
    "actual artifact/output, never the author's claim of success. Criterion: "
    "{criterion}. Respond pass or fail with one reason citing the evidence."
)

#: Selection query for the Verifier's skills (matches `rigorous-review`).
_VERIFY_SKILL_QUERY = "verify validate review audit check evidence correctness"


def _compose_verify_prompt(criterion: str, skills: Optional[SkillRegistry]) -> str:
    """Base verify prompt + any relevant, REVIEWED skills (on-demand injection).

    With no registry the prompt is the inline base (behavior-preserving). With
    one, only skills relevant to validation are selected and only the reviewed
    ones are injected (:func:`runtime.skills.compose_prompt`) — this is how the
    ``rigorous-review`` doctrine reaches the Verifier's prompt.
    """
    base = _VERIFY_PROMPT.format(criterion=criterion)
    if skills is None:
        return base
    return compose_prompt(base, skills.select(_VERIFY_SKILL_QUERY))


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
    skills: Optional[SkillRegistry] = None,
) -> VerifyResult:
    """Independently verify ``result`` against ``task``'s success criterion.

    Returns a :class:`VerifyResult`; the worker acts on it (commit vs re-enqueue).
    ``registry`` must contain the same ``filesystem`` tool (same root) the
    Executor wrote to, so the Verifier reads the real artifact.

    The verdict is decided on **evidence** — the re-read artifact's real contents
    (:func:`_check`), NOT the Executor's ``result.ok`` claim. ``skills`` (optional)
    supplies the ``rigorous-review`` doctrine, injected into the traceability
    prompt; with no registry the prompt is the inline base (behavior-preserving).
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    criterion = payload.get("criterion", "")
    marker = (payload.get("marker") or getattr(result, "marker", None) or "")

    # Model judgement (dry-run, keyless) — logged for traceability. The prompt is
    # the Verifier persona + the relevant reviewed skill(s) (ADR-0008/0014).
    prompt = _compose_verify_prompt(criterion, skills)
    call_model(
        role="verifier",
        task_type="verify",
        messages=[{"role": "user", "content": prompt}],
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
    """Deterministic evidence gate: re-read the artifact and confirm the marker.

    The decision rests on the artifact's REAL contents observed here — never on
    ``result.ok`` (the Executor's claim of success). A result that claims success
    but whose artifact lacks the marker (fails the criterion) still FAILS: evidence
    beats the claim (ADR-0014).
    """
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
