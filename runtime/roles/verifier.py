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

from ..enforce import EventSink, NullEventSink
from ..event_types import EVENT_VERIFY_FAILED, EVENT_VERIFY_PASSED
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..skills import SkillRegistry, emit_skill_applied
from ..tools import ToolRegistry
from .checkers import DEFAULT_REGISTRY, ArtifactRef, CheckerRegistry, resolve_criterion
from .executor import ExecutorResult
from .prompt import compose_role_prompt

#: The verify→commit decision events (``verify.passed`` / ``verify.failed``) are
#: imported from the canonical :mod:`runtime.event_types`.

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


def _compose_verify_prompt(
    criterion: str,
    skills: Optional[SkillRegistry],
    *,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> str:
    """Base verify prompt + charter/overlay + any relevant, REVIEWED skills.

    Assembled through the shared :func:`runtime.roles.prompt.compose_role_prompt`
    so the Verifier layers the same way every role does. With no registry and no
    charter/overlay the prompt is the inline base (behavior-preserving). With a
    registry, only skills relevant to validation are selected and only the reviewed
    ones are injected — this is how the ``rigorous-review`` doctrine (ADR-0014)
    reaches the Verifier's prompt. Charter/overlay are the vertical's config-driven
    framing (default ``None`` → omitted).
    """
    base = _VERIFY_PROMPT.format(criterion=criterion)
    selected = skills.select(_VERIFY_SKILL_QUERY) if skills is not None else None
    return compose_role_prompt(
        base,
        workstream_charter=charter,
        role_overlay=overlay,
        skills=selected,
        budget_aware=True,
    )


class VerifyResult(BaseModel):
    """The gate's verdict.

    ``facts`` carries the concrete evidence the dispatched checker observed
    (ADR-0014) for traceability; it defaults to empty so callers that only read
    ``passed``/``reason`` are unaffected.
    """

    passed: bool
    reason: str
    facts: dict = {}


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
    checkers: CheckerRegistry = DEFAULT_REGISTRY,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
) -> VerifyResult:
    """Independently verify ``result`` against ``task``'s success criterion.

    Returns a :class:`VerifyResult`; the worker acts on it (commit vs re-enqueue).
    ``registry`` must contain the same ``filesystem`` tool (same root) the
    Executor wrote to, so the Verifier reads the real artifact.

    The verdict is decided on **evidence** — the FACTS a dispatched checker
    observes (:func:`_check`), NOT the Executor's ``result.ok`` claim. The
    criterion is structured (``payload["check"] = {"check": name, "require": …}``)
    and dispatched through ``checkers`` (default: the horizontal ``marker`` check);
    a bare marker is back-compat sugar (:func:`runtime.roles.checkers.resolve_criterion`),
    so a vertical injects a domain check (e.g. ``video_audit``) by passing its own
    registry — no Verifier change. ``skills`` (optional) supplies the
    ``rigorous-review`` doctrine; ``charter``/``overlay`` are the vertical's
    config-driven prompt framing (default ``None`` → behavior-preserving).
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    criterion = payload.get("criterion", "")
    marker = (payload.get("marker") or getattr(result, "marker", None) or "")

    # Model judgement (dry-run, keyless) — logged for traceability. The prompt is
    # the Verifier persona + charter/overlay + the relevant reviewed skill(s).
    prompt = _compose_verify_prompt(criterion, skills, charter=charter, overlay=overlay)
    # P0 attribution (ADR-0024): body-free skill.applied for the injected skill(s).
    emit_skill_applied(
        sink, task_id=task.id, role="verifier", workstream=task.workstream,
        skills=skills.select(_VERIFY_SKILL_QUERY) if skills is not None else None,
    )
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

    verdict = _check(conn, task, result, marker, sink, registry, config, checkers)

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
    conn: Any,
    task: Task,
    result: ExecutorResult,
    marker: str,
    sink: EventSink,
    registry: ToolRegistry,
    config: Optional[PolicyConfig],
    checkers: CheckerRegistry,
) -> VerifyResult:
    """Evidence gate: dispatch to the criterion's checker and decide on its FACTS.

    Resolves the (possibly structured) criterion to a ``(check, require)`` pair
    (:func:`runtime.roles.checkers.resolve_criterion`) and runs the registered
    checker. The decision rests on the FACTS the checker OBSERVED — never on
    ``result.ok`` (the Executor's claim). A result that claims success but whose
    artifact fails the check still FAILS: evidence beats the claim (ADR-0014). The
    default ``marker`` checker preserves the historical marker-in-file gate;
    verticals register domain checks on the same seam. An unknown ``check`` name
    raises :class:`runtime.roles.checkers.UnknownChecker` (clear misconfig error).
    """
    payload = task.payload or {}
    name, require = resolve_criterion(payload, fallback_marker=marker)
    ref = ArtifactRef(
        registry=registry,
        path=getattr(result, "artifact_path", None),
        config=config,
        sink=sink,
        result=result,
    )
    outcome = checkers.run(name, conn, task, ref, require)
    return VerifyResult(
        passed=outcome.passed, reason=outcome.reason, facts=outcome.facts
    )
