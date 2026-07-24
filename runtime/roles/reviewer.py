"""Reviewer / Whistle-blower role — the INDEPENDENT risk & disaster guard (ADR-0003).

ADR-0003 names a **Reviewer / Whistle-blower**: an independent guard whose job is
to "spot anything that will lead to failure/disaster" — a *general* reviewer, not
a criterion checker. This role is that guard.

**Reviewer vs Verifier — two different jobs.** The Verifier
(:mod:`runtime.roles.verifier`) is the verify→commit *gate*: it answers ONE narrow
question — does the artifact satisfy *this task's* success criterion? — and its
pass is what lets a work task become ``done``. The Reviewer is a broader,
**after-the-fact** guard: it does NOT gate or block execution (the work is already
committed/failed by the time it runs); instead it scans the finished episode for
**risk / disaster signals** and raises an alarm. The Verifier can PASS a task the
Reviewer still FLAGS (e.g. the criterion was met, but the episode burned 5× its
token budget, or a 🔴 delete kept getting gated). They are deliberately distinct
roles with distinct events.

**Evidence over claims (ADR-0014).** Like every validator, the Reviewer trusts
only evidence it observes itself — it re-reads the target task's ACTUAL event
trail (:func:`runtime.events.read_events`) and its ACTUAL artifact (via the
policy-gated ``invoke(role="reviewer", fs.read)`` — the reviewer role is granted
ONLY ``fs.read``, so it can inspect but never touch). All risk signals are computed
from those **facts**, never from a model's or the author's assertion that things
"look fine". The ``rigorous-review`` skill is injected into the (traceability-only)
model call, but the model's opinion does NOT decide anything — a lying "looks fine"
model changes nothing about the fact-based verdict.

Risk / disaster signals (all fact-derived):

- **hallucinated success** — the trail claims done/verified but the real artifact
  does not back it (missing, unreadable, or lacking the success marker);
- **budget blowout** — ``spent_tokens`` at/over the task ``budget_tokens``;
- **repeated failures / re-kicks** — many ``verify.failed`` / ``work.retry`` /
  ``task.rekicked`` in the trail, or a high ``retries`` count;
- **recurring policy denials** — repeated ``policy.decision`` DENYs in the episode;
- **irreversible / costly actions gated** — ``approval.requested`` (🔴) events, the
  fingerprint of a costly/irreversible action being repeatedly attempted.

Output: a :class:`ReviewResult` (``ok`` | flagged, ``severity``, ``reasons``).
Emits ``review.passed`` / ``review.flagged`` carrying reasons + counts only — never
a secret, arg value, artifact body, or marker. **High** severity escalates: emit
``review.alarm`` (🚨) and raise a 🛑 human approval (:func:`runtime.approvals.request_approval`).
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from ..approvals import request_approval as _request_approval
from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..events import read_events
from ..event_types import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_POLICY_DECISION,
    EVENT_REVIEW_ALARM,
    EVENT_REVIEW_FLAGGED,
    EVENT_REVIEW_PASSED,
    EVENT_TASK_FINISHED,
    EVENT_TASK_REKICKED,
    EVENT_VERIFY_FAILED,
    EVENT_VERIFY_PASSED,
    EVENT_WORK_RETRY,
)
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..skills import SkillRegistry, compose_prompt, emit_skill_applied
from ..tools import ToolRegistry

#: Role verdict/alarm events (``review.passed`` / ``review.flagged``; ``review.alarm``
#: is the 🚨 raised on a HIGH-severity finding, ADR-0006) are imported from the
#: canonical :mod:`runtime.event_types`.

#: The queue task type the worker enqueues to run a review (dispatched to run_review).
REVIEW_TASK_TYPE = "review"

#: Tier on the escalation approval — a 🛑 "approve (blocks)" stakeholder item (ADR-0006).
REVIEW_ESCALATION_TIER = "🛑"
#: Marker used in the 🚨 alarm event payload (ADR-0006).
ALARM_MARK = "🚨"

#: Severity vocabulary + ranking (higher = worse). ``none`` == a clean pass.
SEVERITY_NONE = "none"
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
_SEVERITY_RANK = {SEVERITY_NONE: 0, SEVERITY_LOW: 1, SEVERITY_MEDIUM: 2, SEVERITY_HIGH: 3}

# Base persona. The `rigorous-review` skill (ADR-0014) is composed on top when a
# SkillRegistry is supplied; the model call is traceability-only (its opinion is
# NOT the verdict — the fact-based `assess_risks` decides).
_REVIEW_PROMPT = (
    "You are the studio Reviewer / Whistle-blower, an INDEPENDENT risk guard. Your "
    "job is to spot anything that will lead to failure or disaster — you are NOT "
    "checking the success criterion (the Verifier does that). Judge the finished "
    "episode from EVIDENCE you observe (the event trail + the real artifact), never "
    "from a claim that it 'looks fine'. Target: {target_type}. Report only concrete "
    "risk signals."
)

#: Selection query for the Reviewer's skills (matches `rigorous-review`).
_REVIEW_SKILL_QUERY = "review audit risk whistle-blower evidence correctness validate check"


def _compose_review_prompt(target_type: str, skills: Optional[SkillRegistry]) -> str:
    """Base review prompt + any relevant, REVIEWED skills (on-demand injection).

    With no registry the prompt is the inline base (behavior-preserving); with one,
    only relevant reviewed skills are injected — this is how the ``rigorous-review``
    doctrine reaches the Reviewer's prompt (mirrors PM / Verifier).
    """
    base = _REVIEW_PROMPT.format(target_type=target_type or "work")
    if skills is None:
        return base
    return compose_prompt(base, skills.select(_REVIEW_SKILL_QUERY))


# --- The fact model + the pure risk assessment ------------------------------


class ReviewFacts(BaseModel):
    """The observed FACTS of a finished episode — the only input to the verdict.

    Everything here is evidence the Reviewer gathered itself (event trail + a real
    artifact read + the task's own counters). No model opinion, no author claim.
    """

    #: Whether the trail / outcome asserts the episode succeeded (a claim to test).
    claims_success: bool = False
    #: Whether the episode claimed to produce an artifact (a path was recorded).
    artifact_expected: bool = False
    #: Whether the Reviewer actually read the artifact back (policy-gated fs.read).
    artifact_checked: bool = False
    #: Whether the real artifact backs the success claim (present + marker found).
    artifact_ok: bool = False
    #: Token spend + budget for the target task (budget blowout signal).
    spent_tokens: int = 0
    budget_tokens: Optional[int] = None
    #: Supervisor re-kick count on the target task.
    retries: int = 0
    #: Fact-derived counts from the trail (never bodies): failure/retry/re-kick
    #: signals, policy DENYs, and 🔴 approval-gated actions.
    fail_signals: int = 0
    deny_count: int = 0
    pend_count: int = 0


class RiskSignal(BaseModel):
    """One fact-derived risk finding: a severity + a leak-free human reason."""

    severity: str
    reason: str


class ReviewResult(BaseModel):
    """The Reviewer's verdict (returned to the worker for the task result)."""

    ok: bool
    severity: str = SEVERITY_NONE
    reasons: list[str] = Field(default_factory=list)
    #: Set when a HIGH finding raised a 🛑 approval (escalation).
    approval_id: Optional[str] = None
    target_task_id: Optional[str] = None


def _worst(severities: list[str]) -> str:
    """Return the highest-ranked severity, or ``none`` for an empty list."""
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0), default=SEVERITY_NONE)


def assess_risks(facts: ReviewFacts) -> list[RiskSignal]:
    """Derive risk signals from observed FACTS — pure, deterministic, DB/model-free.

    This is the Reviewer's actual judgement: every signal is computed from evidence
    (:class:`ReviewFacts`), never from a claim. Reasons carry counts/numbers only —
    no secret, arg value, artifact body, or marker text — so they are safe to log.
    """
    signals: list[RiskSignal] = []

    # 1. Hallucinated success — a done/verified claim not backed by the artifact.
    #    Evidence beats the claim: we only fire when we can REFUTE it from facts.
    if facts.claims_success:
        if not facts.artifact_expected:
            signals.append(RiskSignal(
                severity=SEVERITY_HIGH,
                reason="success claimed but no artifact was produced (unbacked done claim)",
            ))
        elif facts.artifact_checked and not facts.artifact_ok:
            signals.append(RiskSignal(
                severity=SEVERITY_HIGH,
                reason="success claimed but the real artifact does not back it "
                       "(missing / unreadable / success marker absent)",
            ))

    # 2. Budget blowout — spend at or over the task's token budget.
    budget = facts.budget_tokens
    if budget is not None and budget > 0:
        if facts.spent_tokens > budget:
            signals.append(RiskSignal(
                severity=SEVERITY_HIGH,
                reason=f"token spend {facts.spent_tokens} exceeded budget {budget}",
            ))
        elif facts.spent_tokens >= 0.9 * budget:
            signals.append(RiskSignal(
                severity=SEVERITY_MEDIUM,
                reason=f"token spend {facts.spent_tokens} near budget {budget} (>=90%)",
            ))

    # 3. Repeated failures / re-kicks — instability the criterion check can't see.
    fail_score = facts.fail_signals + facts.retries
    if fail_score >= 4:
        signals.append(RiskSignal(
            severity=SEVERITY_HIGH,
            reason=f"repeated failures/re-kicks in the episode ({fail_score} signals)",
        ))
    elif fail_score >= 2:
        signals.append(RiskSignal(
            severity=SEVERITY_MEDIUM,
            reason=f"multiple failures/re-kicks in the episode ({fail_score} signals)",
        ))

    # 4. Recurring policy denials — least-privilege kept refusing a call.
    if facts.deny_count >= 2:
        signals.append(RiskSignal(
            severity=SEVERITY_MEDIUM,
            reason=f"recurring policy denials in the episode ({facts.deny_count})",
        ))
    elif facts.deny_count == 1:
        signals.append(RiskSignal(
            severity=SEVERITY_LOW,
            reason="a policy denial occurred in the episode (1)",
        ))

    # 5. Irreversible / costly actions gated — 🔴 approval-gated attempts.
    if facts.pend_count >= 2:
        signals.append(RiskSignal(
            severity=SEVERITY_MEDIUM,
            reason=f"recurring approval-gated 🔴 (costly/irreversible) actions ({facts.pend_count})",
        ))
    elif facts.pend_count == 1:
        signals.append(RiskSignal(
            severity=SEVERITY_LOW,
            reason="an approval-gated 🔴 (costly/irreversible) action was attempted (1)",
        ))

    return signals


# --- Trail evidence extraction ----------------------------------------------


def _gather_facts_from_trail(
    events: list, *, outcome: str, artifact_expected: bool,
    artifact_checked: bool, artifact_ok: bool,
    spent_tokens: int, budget_tokens: Optional[int], retries: int,
) -> ReviewFacts:
    """Fold an event trail + task counters into :class:`ReviewFacts` (evidence)."""
    types = [getattr(e, "type", "") for e in events]

    # A success CLAIM (to be tested), read from the trail or the recorded outcome.
    finished_done = any(
        getattr(e, "type", "") == EVENT_TASK_FINISHED
        and (getattr(e, "payload", {}) or {}).get("status") == "done"
        for e in events
    )
    claims_success = outcome == "done" or EVENT_VERIFY_PASSED in types or finished_done

    fail_signals = (
        types.count(EVENT_VERIFY_FAILED) + types.count(EVENT_WORK_RETRY) + types.count(EVENT_TASK_REKICKED)
    )
    deny_count = sum(
        1 for e in events
        if getattr(e, "type", "") == EVENT_POLICY_DECISION
        and (getattr(e, "payload", {}) or {}).get("effect") == "deny"
    )
    pend_count = types.count(EVENT_APPROVAL_REQUESTED)

    return ReviewFacts(
        claims_success=claims_success,
        artifact_expected=artifact_expected,
        artifact_checked=artifact_checked,
        artifact_ok=artifact_ok,
        spent_tokens=spent_tokens,
        budget_tokens=budget_tokens,
        retries=retries,
        fail_signals=fail_signals,
        deny_count=deny_count,
        pend_count=pend_count,
    )


def _read_artifact_evidence(
    conn: Any, task: Task, artifact_path: str, marker: str, sink: EventSink,
    registry: ToolRegistry, config: Optional[PolicyConfig],
) -> tuple[bool, bool]:
    """Independently read the target artifact via the policy gate (reviewer→fs.read).

    Returns ``(checked, ok)``: ``checked`` is whether the read executed at all; ``ok``
    is whether the real contents back the success claim (contain ``marker``; or, with
    no marker defined, are simply non-empty). This is the Reviewer's own evidence —
    it never trusts the Executor's ``ok`` flag.
    """
    read = invoke(
        role="reviewer",
        tool_name="filesystem",
        registry=registry,
        config=config,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="read",
        path=artifact_path,
    )
    if read.status is not InvokeStatus.EXECUTED or not (read.result and read.result.ok):
        return True, False  # we tried to read the claimed artifact and could not
    content = read.result.output or ""
    ok = (marker in content) if marker else bool(content.strip())
    return True, ok


def run_review(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    registry: Optional[ToolRegistry] = None,
    config: Optional[PolicyConfig] = None,
    model_registry: Optional[Registry] = None,
    skills: Optional[SkillRegistry] = None,
    read: Callable[..., list] = read_events,
    request_approval: Callable[..., Any] = _request_approval,
) -> ReviewResult:
    """Review the finished episode referenced by ``task.payload`` for risk/disaster.

    ``task`` is a ``review`` queue task carrying ``target_task_id`` / ``outcome`` and
    the target's facts (``artifact_path``, ``marker``, ``spent_tokens``,
    ``budget_tokens``, ``retries``). It reads the target's ACTUAL event trail and (if
    ``registry`` is supplied) its ACTUAL artifact via the policy-gated
    ``invoke(role="reviewer", fs.read)``, computes fact-based risk signals
    (:func:`assess_risks`), and emits ``review.passed`` / ``review.flagged``. A HIGH
    finding escalates: ``review.alarm`` (🚨) + a 🛑 approval. It NEVER enqueues another
    task (no review-loop) and NEVER blocks execution — it is an after-the-fact guard.

    ``read`` / ``request_approval`` are injectable for tests; ``call_model`` is a
    module attribute so tests can monkeypatch a lying "looks fine" model and prove
    the verdict still rests on facts.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    target_id = payload.get("target_task_id")
    target_type = payload.get("target_task_type") or "work"
    outcome = payload.get("outcome") or ""
    artifact_path = payload.get("artifact_path")
    marker = payload.get("marker") or ""
    spent_tokens = int(payload.get("spent_tokens") or 0)
    budget_tokens = payload.get("budget_tokens")
    retries = int(payload.get("retries") or 0)

    # 1. Read the target episode's replayable trail (deterministic seq order).
    events: list = []
    if target_id:
        try:
            events = read(conn, task_id=UUID(str(target_id)))
        except Exception:  # pragma: no cover - malformed id → fall back to workstream
            events = []
    if not events:
        events = read(conn, workstream=task.workstream)

    # 2. Independently read the artifact (evidence over the Executor's claim). Only
    #    possible with a tool registry; otherwise the hallucination check stays
    #    UNVERIFIED (never a false flag).
    artifact_expected = bool(artifact_path)
    artifact_checked = False
    artifact_ok = False
    if registry is not None and artifact_path:
        artifact_checked, artifact_ok = _read_artifact_evidence(
            conn, task, artifact_path, marker, sink, registry, config
        )

    # 3. Traceability-only model call — persona + injected `rigorous-review`
    #    doctrine (ADR-0008/0014). Its opinion does NOT decide the verdict.
    prompt = _compose_review_prompt(target_type, skills)
    # P0 attribution (ADR-0024): body-free skill.applied for the injected skill(s).
    emit_skill_applied(
        sink, task_id=task.id, role="reviewer", workstream=task.workstream,
        skills=skills.select(_REVIEW_SKILL_QUERY) if skills is not None else None,
    )
    call_model(
        role="reviewer",
        task_type="review",
        messages=[{"role": "user", "content": prompt}],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # 4. Assess risk from the FACTS (pure, deterministic, evidence-based).
    facts = _gather_facts_from_trail(
        events, outcome=outcome, artifact_expected=artifact_expected,
        artifact_checked=artifact_checked, artifact_ok=artifact_ok,
        spent_tokens=spent_tokens, budget_tokens=budget_tokens, retries=retries,
    )
    signals = assess_risks(facts)
    severity = _worst([s.severity for s in signals])
    reasons = [s.reason for s in signals]

    # 5. Clean → review.passed and return (an after-the-fact guard, no blocking).
    if not signals:
        sink.emit(make_event(
            workstream=task.workstream,
            type=EVENT_REVIEW_PASSED,
            task_id=task.id,
            payload={"target_task_id": str(target_id) if target_id else None,
                     "target_task_type": target_type, "severity": SEVERITY_NONE},
        ))
        return ReviewResult(ok=True, severity=SEVERITY_NONE, reasons=[],
                            target_task_id=str(target_id) if target_id else None)

    # Flagged — emit reasons + counts only (NO secret / arg / body / marker).
    sink.emit(make_event(
        workstream=task.workstream,
        type=EVENT_REVIEW_FLAGGED,
        task_id=task.id,
        payload={"target_task_id": str(target_id) if target_id else None,
                 "target_task_type": target_type, "severity": severity,
                 "signal_count": len(signals), "reasons": reasons},
    ))

    approval_id: Optional[str] = None
    if severity == SEVERITY_HIGH:
        # Escalate: 🚨 alarm + a 🛑 human approval (ADR-0006). Both carry the same
        # leak-free reasons. The approval blocks a human's attention, not the queue.
        sink.emit(make_event(
            workstream=task.workstream,
            type=EVENT_REVIEW_ALARM,
            task_id=task.id,
            payload={"target_task_id": str(target_id) if target_id else None,
                     "mark": ALARM_MARK, "severity": severity,
                     "signal_count": len(signals), "reasons": reasons},
        ))
        approval = request_approval(
            conn,
            task_id=task.id,
            role="reviewer",
            tool="review",
            capabilities=[],
            tier=REVIEW_ESCALATION_TIER,
            reason="reviewer flagged high-severity risk: " + "; ".join(reasons),
            sink=sink,
            workstream=task.workstream,
        )
        approval_id = str(approval.id) if approval is not None else None

    return ReviewResult(
        ok=False, severity=severity, reasons=reasons, approval_id=approval_id,
        target_task_id=str(target_id) if target_id else None,
    )
