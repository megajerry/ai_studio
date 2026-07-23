"""Critic role — the FORWARD-looking adversarial partner on decisions (ADR-0003/0019).

The **Reviewer / Whistle-blower** (:mod:`runtime.roles.reviewer`) is an
*after-the-fact* evidence guard on **completed** work: it scans a finished episode
for risk/disaster signals it can observe. The **Critic** is the mirror image —
a *before-the-fact* adversarial partner on **decisions**: given a proposed
plan / decision / distilled-lesson set, it tries to find what is **wrong or
missing** *before* the studio commits to it. Its job is to disagree productively,
not to agree.

Pattern (ADR-0019): **PM proposes → Critic critiques → PM drives to consensus
(revise or justify) → converge, or escalate a genuine disagreement to the
stakeholder (🛑).** The Critic itself is one half of that loop; the PM↔Critic
consensus driver lives in :mod:`runtime.roles.pm`.

A critique is a set of :class:`Concern` s, each classified by ``kind``:

- **risk** — a way the decision could fail or cause harm;
- **downside** — a real cost/tradeoff the proposal underweights;
- **missed_opportunity** — value the proposal leaves on the table;
- **alternative** — a different approach worth weighing.

From the concerns the Critic derives a ``blocking`` flag and a
``recommendation`` ∈ {``proceed``, ``revise``, ``escalate``}.

Like the Reviewer, the Critic critiques **from evidence + reasoning, not vibes**
(ADR-0014): its verdict is computed deterministically from the STRUCTURED FACTS of
the subject (:func:`assess_concerns`), so it is reproducible and testable keyless.
It makes a ``call_model(role="critic", task_type="critique")`` dry-run call for
traceability (routed/costed/logged like any model call), but that call's text does
**not** decide anything — a lying "looks great" model changes nothing.

It emits ``critic.reviewed`` carrying only COUNTS / kinds / severities /
recommendation — never a plan body, lesson text, secret, or concern-statement
prose that could leak the subject (CLAUDE.md invariants 5 & 6).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from ..enforce import EventSink, NullEventSink
from ..event_types import EVENT_CRITIC_REVIEWED
from ..model.call import call_model as _call_model
from ..model.registry import Registry
from ..models import make_event
from ..skills import SkillRegistry
from ..trajectory import add_step
from .prompt import compose_role_prompt

log = logging.getLogger("runtime.roles.critic")

# --- Vocabulary -------------------------------------------------------------

#: Concern kinds — the four things a forward critic looks for (ADR-0019).
KIND_RISK = "risk"
KIND_DOWNSIDE = "downside"
KIND_MISSED_OPPORTUNITY = "missed_opportunity"
KIND_ALTERNATIVE = "alternative"
CONCERN_KINDS = frozenset(
    {KIND_RISK, KIND_DOWNSIDE, KIND_MISSED_OPPORTUNITY, KIND_ALTERNATIVE}
)

#: Severity vocabulary + ranking (higher = worse). Mirrors the Reviewer's scale.
SEVERITY_NONE = "none"
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
_SEVERITY_RANK = {SEVERITY_NONE: 0, SEVERITY_LOW: 1, SEVERITY_MEDIUM: 2, SEVERITY_HIGH: 3}

#: Recommendations the Critic returns.
CRITIC_PROCEED = "proceed"
CRITIC_REVISE = "revise"
CRITIC_ESCALATE = "escalate"

#: A HIGH-severity concern blocks — the PM must revise or justify before commit.
_BLOCKING_SEVERITY = SEVERITY_HIGH

# Base persona. On-demand skills (ADR-0008) + a vertical's charter/overlay are
# composed on top via `compose_role_prompt`. The model call is traceability-only;
# the fact-based `assess_concerns` decides the verdict.
_CRITIC_PROMPT = (
    "You are the studio Critic — a FORWARD-looking adversarial partner on a "
    "decision the PM is about to commit to (a plan, a major choice, or a set of "
    "distilled lessons). Your job is to find what is WRONG or MISSING before it is "
    "committed, NOT to agree. Look for: risks (ways it fails/harms), downsides "
    "(costs it underweights), missed opportunities (value left on the table), and "
    "alternatives (better approaches). Critique from evidence and reasoning, never "
    "from vibes. Subject: {subject}."
)

#: Selection query for the Critic's skills (evidence/skepticism doctrine).
_CRITIC_SKILL_QUERY = "critique risk downside alternative decision review evidence skepticism"


# --- The critique contract --------------------------------------------------


class Concern(BaseModel):
    """One forward-looking concern the Critic raises about a proposal.

    ``kind`` is one of :data:`CONCERN_KINDS`; ``severity`` one of the severity
    vocabulary. ``statement`` + ``rationale`` are human-readable (they may name the
    subject, so they are NEVER placed on an event — the event carries counts only).
    ``code`` is an optional machine-readable tag the consensus loop can act on.
    """

    kind: str
    severity: str = SEVERITY_MEDIUM
    statement: str = ""
    rationale: str = ""
    code: str = ""


class Critique(BaseModel):
    """The Critic's structured verdict on a proposal.

    ``concerns`` is what it found; ``blocking`` is set when at least one HIGH
    concern means the PM should not commit without addressing it; ``recommendation``
    is the Critic's call: ``proceed`` (nothing blocking), ``revise`` (blocking but
    addressable), or ``escalate`` (a genuine disagreement for the stakeholder).
    """

    subject_kind: str = ""
    concerns: list[Concern] = Field(default_factory=list)
    blocking: bool = False
    recommendation: str = CRITIC_PROCEED


def _worst(severities: list[str]) -> str:
    """The highest-ranked severity in the list, or ``none`` when empty."""
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0), default=SEVERITY_NONE)


def _kind_counts(concerns: list[Concern]) -> dict[str, int]:
    """Count concerns by kind (leak-free — kinds are a closed vocabulary)."""
    counts: dict[str, int] = {}
    for c in concerns:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return counts


def _severity_counts(concerns: list[Concern]) -> dict[str, int]:
    """Count concerns by severity (leak-free)."""
    counts: dict[str, int] = {}
    for c in concerns:
        counts[c.severity] = counts.get(c.severity, 0) + 1
    return counts


def _record_consult(
    conn: Any,
    trajectory_id: Optional[UUID],
    kind: str,
    concerns: list[Concern],
    blocking: bool,
    recommendation: str,
) -> None:
    """Record this critique as a ``consult`` step on an open PM trajectory (ADR-0020).

    Observe-only + DB-outage-safe (ADR-0017): with no ``conn``/``trajectory_id`` or
    on ANY failure it logs + returns, so critiquing NEVER blocks or crashes. The
    verdict lands in ``choice``/``refs`` and the full concern bodies (which may name
    the subject) in the VERBATIM ``rationale`` — that lives in the local DB only, so
    it is safe here (unlike the body-free ``critic.reviewed`` event).
    """
    if conn is None or trajectory_id is None:
        return
    rationale = "\n".join(
        f"- [{c.severity}/{c.kind}] {c.statement}: {c.rationale}" for c in concerns
    ) or "no concerns raised"
    try:
        add_step(
            conn, trajectory_id, "consult",
            f"critiqued the {kind} ({len(concerns)} concern(s)) → {recommendation}",
            rationale=rationale,
            choice=recommendation,
            refs={
                "blocking": blocking,
                "recommendation": recommendation,
                "concern_count": len(concerns),
                "kinds": _kind_counts(concerns),
                "severities": _severity_counts(concerns),
            },
        )
    except Exception:  # pragma: no cover - defensive: recording is never load-bearing
        log.warning("Critic trajectory consult step failed; proceeding", exc_info=True)


# --- Fact-based assessment (pure, deterministic, DB/model-free) --------------


def _assess_plan(ctx: dict) -> list[Concern]:
    """Concerns for a PM ``plan`` proposal, from its structured facts.

    ``ctx`` carries only NUMBERS/flags describing the plan (never bodies): item
    count, criteria count, how many items lack a checkable criterion/marker, the
    self-scored confidence, and whether any dependency edges were declared.
    """
    concerns: list[Concern] = []
    n_items = int(ctx.get("n_items", 0))
    n_crit = int(ctx.get("n_success_criteria", 0))
    missing_crit = int(ctx.get("items_missing_criterion", 0))
    missing_marker = int(ctx.get("items_missing_marker", 0))
    confidence = float(ctx.get("confidence", 0.0))
    has_deps = bool(ctx.get("has_dependencies", False))

    # RISK (HIGH, blocking): no measurable success criteria at all → nothing to
    # verify the goal against; the studio would "succeed" without proof.
    if n_crit == 0:
        concerns.append(Concern(
            kind=KIND_RISK, severity=SEVERITY_HIGH, code="no_success_criteria",
            statement="the plan defines no measurable, independently checkable success criteria",
            rationale="with no criterion there is no evidence the goal was actually met",
        ))
    # RISK (HIGH, blocking): work items with no checkable criterion of their own.
    if missing_crit:
        concerns.append(Concern(
            kind=KIND_RISK, severity=SEVERITY_HIGH, code="items_missing_criterion",
            statement=f"{missing_crit} work item(s) lack an independently checkable success criterion",
            rationale="an item with no criterion cannot be verified against a real artifact",
        ))
    # RISK (MEDIUM): items with no success marker (weaker than a missing criterion).
    if missing_marker and not missing_crit:
        concerns.append(Concern(
            kind=KIND_RISK, severity=SEVERITY_MEDIUM, code="items_missing_marker",
            statement=f"{missing_marker} work item(s) have no success marker for the evidence gate",
            rationale="without a marker the deterministic verify step falls back to a default",
        ))
    # DOWNSIDE (LOW): overconfidence — a high self-score on a single coarse criterion.
    if confidence >= 0.85 and n_crit <= 1 and n_items >= 2:
        concerns.append(Concern(
            kind=KIND_DOWNSIDE, severity=SEVERITY_LOW, code="thin_criteria",
            statement="high self-confidence rests on a single aggregate success criterion",
            rationale="one coarse criterion can mask partial failure of individual items",
        ))
    # MISSED_OPPORTUNITY (LOW): many items, no declared dependencies — confirm the
    # parallelism is real (unsequenced-but-actually-dependent items cause rework).
    if n_items >= 3 and not has_deps:
        concerns.append(Concern(
            kind=KIND_MISSED_OPPORTUNITY, severity=SEVERITY_LOW, code="no_dependencies",
            statement="all work items are declared independent; confirm none must precede another",
            rationale="items that actually depend on each other but run in parallel cause rework",
        ))
    # ALTERNATIVE (MEDIUM): large scope in one batch — weigh a smaller first phase.
    if n_items >= 5:
        concerns.append(Concern(
            kind=KIND_ALTERNATIVE, severity=SEVERITY_MEDIUM, code="phase_scope",
            statement="consider delivering a smaller first phase before the full decomposition",
            rationale="a large single batch delays feedback and widens the blast radius",
        ))
    return concerns


def _assess_lessons(ctx: dict) -> list[Concern]:
    """Concerns for a set of distilled retro ``lessons``, from their facts.

    ``ctx`` carries only counts/flags: how many lessons, the episode outcome, and
    whether a failed episode produced a prevention-oriented lesson.
    """
    concerns: list[Concern] = []
    n_lessons = int(ctx.get("n_lessons", 0))
    outcome = str(ctx.get("outcome", ""))
    has_prevention = bool(ctx.get("has_prevention_lesson", False))

    # RISK (HIGH, blocking): the retro distilled nothing durable to carry forward.
    if n_lessons == 0:
        concerns.append(Concern(
            kind=KIND_RISK, severity=SEVERITY_HIGH, code="no_lessons",
            statement="the retro distilled no durable lessons from the episode",
            rationale="a retro that stores nothing cannot improve future work",
        ))
    # MISSED_OPPORTUNITY (MEDIUM): a FAILED episode with no prevention lesson.
    if outcome == "failed" and n_lessons and not has_prevention:
        concerns.append(Concern(
            kind=KIND_MISSED_OPPORTUNITY, severity=SEVERITY_MEDIUM, code="no_prevention_lesson",
            statement="a failed episode produced no prevention-oriented lesson",
            rationale="the most valuable lesson from a failure is how to prevent a recurrence",
        ))
    return concerns


def assess_concerns(subject_kind: str, context: dict) -> list[Concern]:
    """Derive concerns from a proposal's STRUCTURED FACTS — pure & deterministic.

    Dispatches on ``subject_kind`` (``plan`` / ``lessons``); an unknown kind yields
    no concerns (the Critic has no fact-based basis to object). This is the Critic's
    actual judgement — reproducible, testable, and independent of the model call.
    """
    if subject_kind == "plan":
        return _assess_plan(context or {})
    if subject_kind == "lessons":
        return _assess_lessons(context or {})
    return []


def decide(concerns: list[Concern]) -> tuple[bool, str]:
    """Fold concerns into a ``(blocking, recommendation)`` verdict.

    Only a HIGH-severity concern blocks; a blocking critique recommends ``revise``
    (the PM should address it or justify). ``escalate`` is reserved for the PM↔Critic
    consensus loop, which escalates a genuine, unresolved disagreement — the Critic
    alone recommends ``proceed`` or ``revise``.
    """
    if not concerns:
        return False, CRITIC_PROCEED
    worst = _worst([c.severity for c in concerns])
    if _SEVERITY_RANK.get(worst, 0) >= _SEVERITY_RANK[_BLOCKING_SEVERITY]:
        return True, CRITIC_REVISE
    return False, CRITIC_PROCEED


def run_critic(
    subject: str,
    context: Optional[dict] = None,
    *,
    sink: Optional[EventSink] = None,
    conn: Any = None,
    task_id: Optional[UUID] = None,
    workstream: str = "productivity",
    subject_kind: Optional[str] = None,
    registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    charter: Optional[str] = None,
    overlay: Optional[str] = None,
    trajectory_id: Optional[UUID] = None,
    call_model: Callable[..., Any] = _call_model,
    assess: Callable[[str, dict], list[Concern]] = assess_concerns,
) -> Critique:
    """Critique a proposed decision and return a structured :class:`Critique`.

    ``subject`` is a short human label (used only in the prompt, never on an event);
    ``context`` carries the STRUCTURED FACTS the verdict is computed from (numbers /
    flags only — never plan/lesson bodies). ``subject_kind`` selects the fact-based
    assessor (defaults to ``context["kind"]``, else ``"plan"``).

    A traceability-only ``call_model(role="critic", task_type="critique")`` dry-run
    call is made (routed/costed/logged) but its text does NOT decide anything — the
    verdict is the pure ``assess`` over the facts (ADR-0014). Emits ``critic.reviewed``
    with COUNTS / kinds / severities / recommendation only. ``call_model`` / ``assess``
    are injectable for tests.

    ``trajectory_id`` optionally links this critique to an open PM trajectory
    (ADR-0020): when set, the verdict + verbatim concerns are recorded as a
    ``consult`` step (observe-only, DB-outage-safe) so the PM↔Critic loop is
    replayable. It changes nothing about the returned verdict.
    """
    sink = sink or NullEventSink()
    context = context or {}
    kind = subject_kind or str(context.get("kind") or "plan")

    # Traceability-only model call — persona + charter/overlay + any reviewed skills
    # (ADR-0008). Keyless dry-run; its opinion is NOT the verdict.
    selected = skills.select(_CRITIC_SKILL_QUERY) if skills is not None else None
    prompt = compose_role_prompt(
        _CRITIC_PROMPT.format(subject=subject or kind),
        workstream_charter=charter,
        role_overlay=overlay,
        skills=selected,
    )
    call_model(
        role="critic",
        task_type="critique",
        messages=[{"role": "user", "content": prompt}],
        quality="high",
        registry=registry,
        conn=conn,
        task_id=task_id,
        sink=sink,
        workstream=workstream,
    )

    # The verdict: pure, fact-based concern assessment (deterministic, testable).
    concerns = [c for c in assess(kind, context) if c.kind in CONCERN_KINDS]
    blocking, recommendation = decide(concerns)
    critique = Critique(
        subject_kind=kind, concerns=concerns,
        blocking=blocking, recommendation=recommendation,
    )

    # Record the consult on the PM's trajectory (verbatim concerns; local DB only).
    _record_consult(conn, trajectory_id, kind, concerns, blocking, recommendation)

    # Emit critic.reviewed — COUNTS / kinds / severities / recommendation only.
    # NEVER a concern statement, rationale, plan body, or lesson text.
    sink.emit(make_event(
        workstream=workstream,
        type=EVENT_CRITIC_REVIEWED,
        task_id=task_id,
        payload={
            "subject_kind": kind,
            "concern_count": len(concerns),
            "kinds": _kind_counts(concerns),
            "severities": _severity_counts(concerns),
            "blocking": blocking,
            "recommendation": recommendation,
        },
    ))
    return critique
