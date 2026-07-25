"""Skill-lifecycle role — turn the efficacy verdict into a human-gated
DEPRECATION / REVISION proposal for a LIVE skill that isn't helping (ADR-0024 P4).

P0/P1 made skill USAGE attributable (``skill.applied``) and measured EFFICACY
(:func:`runtime.quality.skill_efficacy_report`); P2's Curator INDUCED new candidate
skills. This role closes the keep/tune/retire half the stakeholder asked for — "when
skills are applied, this data is useful to see if skills should be adjusted /
optimized". It reads the per-live-skill verdict
(:func:`runtime.quality.skill_lifecycle_verdicts`) and, for a LIVE
(``reviewed:true``) skill whose applied cohort shows NO benefit (``revise``) or a
CONFIDENT degradation vs baseline (``retire``):

1. **PROPOSES a deprecation / revision candidate** — a written, REVIEWABLE proposal
   artifact through the policy-gated filesystem tool, to a confined review path
   (``proposals/skills/<slug>.md``). It is a ``reviewed:false``-style candidate for a
   HUMAN to act on, EXACTLY the :mod:`runtime.roles.curator` /
   :mod:`runtime.roles.failure_analyst` discipline. A role without ``fs.write`` is
   DENIED (nothing written) — a safe, logged no-op.
2. **Emits body-free telemetry** — ``skill.deprecation_proposed`` (verdict
   ``retire``) / ``skill.revision_proposed`` (verdict ``revise``) carrying ONLY the
   skill NAME + verdict + driving task_type family + the first-pass-merge rate/delta +
   n + Wilson CI + efficiency deltas + thresholds — NEVER a skill's instruction body
   or any prompt/secret text (invariants 5 & 6).

It acts through exactly the sanctioned seams — never agent-direct (architecture §9,
CLAUDE.md invariants 1-3):

- **Reads only** the efficacy report + verdict (derived from the append-only event
  log + ``task_transitions`` + ``trajectory_steps``; no new capture, replayable).
- **Any file write via the policy-gated tool layer** (never a direct host write).

Invariants it upholds:

- **NEVER auto-retires.** It NEVER removes or edits a live skill file and NEVER flips
  ``reviewed``. The proposal is a candidate; deprecating/revising a live skill is a
  separate, human-gated step (``auto_retired`` is always false on the wire).
- **Never fires on thin evidence.** The verdict itself only reaches ``retire`` /
  ``revise`` at ``n ≥ min_sample`` with a confident degradation / no-benefit signal
  (Wilson CI, pooled per task_type family); a tiny/insufficient sample is
  ``keep``/``insufficient`` and proposes NOTHING (statistical-rigor doctrine).
- **No loop.** A lifecycle task enqueues nothing — it proposes and stops.
- **Only LIVE skills.** It judges only ``reviewed:true`` skills from the registry;
  candidate/unknown applied skills are never judged (so it never "deprecates" a P2
  candidate).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..event_types import (
    EVENT_SKILL_DEPRECATION_PROPOSED,
    EVENT_SKILL_REVISION_PROPOSED,
)
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..quality import (
    DEFAULT_EFFICIENCY_FLOOR,
    MIN_TRUSTWORTHY_SAMPLE,
    VERDICT_RETIRE,
    VERDICT_REVISE,
    skill_efficacy_report as _skill_efficacy_report,
    skill_lifecycle_verdicts as _skill_lifecycle_verdicts,
)
from ..skills import SkillRegistry
from ..tools import ToolRegistry

log = logging.getLogger("runtime.roles.skill_lifecycle")

#: The queue task types the worker dispatches to :func:`run_skill_lifecycle`.
SKILL_LIFECYCLE_TASK_TYPES = ("skill_lifecycle", "tune.skills")

#: The role name the policy gate checks (must be granted ``fs.write`` to write the
#: reviewable proposal; without it the write is DENIED — a safe, logged no-op).
ROLE = "skill_lifecycle"

#: Default review directory (under the confined tool root; git-ignored). NOT the live
#: ``skills/`` root — acting on a proposal is a separate, reviewed, human-gated step.
DEFAULT_PROPOSALS_DIR = "proposals/skills"

#: Hard cap on proposals per task — bounds fan-out (one proposal per underperformer).
MAX_PROPOSALS = 4

#: A proposal is ALWAYS an unreviewed, un-adopted candidate (mirrors the Curator's
#: ``CANDIDATE_REVIEWED`` / Sourcing discipline). Emitted on the wire so a consumer can
#: never mistake a proposal for an applied retirement.
PROPOSAL_REVIEWED = False

#: The two underperformance verdicts that earn a proposal, mapped to the human ACTION
#: + its body-free event type.
_ACTIONS = {
    VERDICT_RETIRE: ("deprecate", EVENT_SKILL_DEPRECATION_PROPOSED),
    VERDICT_REVISE: ("revise", EVENT_SKILL_REVISION_PROPOSED),
}


def _slug(skill_name: str) -> str:
    """A filesystem-safe slug for a skill name (no ``/`` or ``:`` in a path)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", skill_name).strip("_") or "skill"


# ===========================================================================
# Result model
# ===========================================================================


class ProposedSkillAction(BaseModel):
    """One underperforming LIVE skill → its human-gated deprecation/revision proposal.

    Counts / names / rates only — never a skill's instruction body (invariants 5 & 6).
    """

    skill: str
    #: The keep/tune/retire verdict that earned the proposal: ``retire`` | ``revise``.
    verdict: str
    #: The human ACTION the proposal frames: ``deprecate`` | ``revise``.
    action: str
    driving_family: Optional[str] = None
    reason: str = ""
    applied_first_pass_rate: Optional[float] = None
    baseline_first_pass_rate: Optional[float] = None
    first_pass_delta: Optional[float] = None
    applied_ci95: Optional[tuple[float, float]] = None
    applied_n: Optional[int] = None
    baseline_n: Optional[int] = None
    efficiency_delta: dict = Field(default_factory=dict)
    #: Path of the LIVE skill file the proposal concerns (read-only reference; the
    #: file is NEVER edited/removed here). ``None`` when the registry has no path.
    live_skill_path: Optional[str] = None
    #: Proposal-write outcome: "off" | "executed" | "denied" | "pending".
    proposal_status: str = "off"
    #: Path (tool-root-relative) of the written proposal, if written.
    proposal_path: Optional[str] = None
    #: A proposal is ALWAYS an unreviewed, un-adopted candidate (review-before-act).
    reviewed: bool = PROPOSAL_REVIEWED


class SkillLifecycleResult(BaseModel):
    """What one skill-lifecycle task produced (returned to the worker as the result).

    ``proposals_made`` is 0 when every LIVE skill is keeping/insufficient (nothing
    proposed, nothing emitted, no live skill touched).
    """

    workstream: str
    skills_judged: int
    #: Verdict counts across the judged live skills (retire/revise/keep/insufficient).
    verdicts: dict = Field(default_factory=dict)
    proposals_made: int = 0
    proposals: list[ProposedSkillAction] = Field(default_factory=list)
    min_sample: int
    efficiency_floor: float


# ===========================================================================
# Reviewable proposal artifact
# ===========================================================================


def render_proposal(entry: dict, action: str, *, live_path: Optional[str]) -> str:
    """Render the REVIEWABLE deprecation/revision proposal (Markdown) — the analogue
    of the Curator's candidate ``SKILL.md``.

    Describes the LIVE skill, its verdict + evidence (first-pass-merge applied vs
    baseline with n + Wilson CI, efficiency deltas), and states plainly that the live
    skill was NOT modified/removed — a human decides. ``reviewed: false`` frontmatter
    so nothing can mistake it for an applied action.
    """
    name = entry["skill"]
    verdict = entry["verdict"]
    fam = entry.get("driving_family")
    fv = next((f for f in entry.get("by_family", [])
               if f["task_family"] == fam), None) or {}
    a_rate = fv.get("applied_first_pass_rate")
    a_ci = fv.get("applied_first_pass_ci95")
    a_n = fv.get("applied_n")
    b_rate = fv.get("baseline_first_pass_rate")
    b_ci = fv.get("baseline_first_pass_ci95")
    b_n = fv.get("baseline_n")
    headline = ("DEPRECATE (retire)" if verdict == VERDICT_RETIRE
                else "REVISE (tune)")
    return (
        "---\n"
        f"name: {_slug(name)}-{action}\n"
        f"description: PROPOSED {action} of the live skill '{name}' — its applied "
        f"cohort shows {'a confident degradation' if verdict == VERDICT_RETIRE else 'no measurable benefit'} "
        "vs baseline.\n"
        f"target_skill: {name}\n"
        f"verdict: {verdict}\n"
        "reviewed: false\n"
        "source: skill_lifecycle\n"
        "---\n\n"
        f"# PROPOSED {headline} — skill `{name}` (CANDIDATE — NOT applied)\n\n"
        "Produced by the Skill-lifecycle role (ADR-0024 P4). REVIEW before acting;\n"
        "deprecating/revising the live skill is a separate, human-gated step. This\n"
        "artifact ONLY proposes — the live skill file was NEVER edited or removed.\n\n"
        f"- live skill file (unchanged): `{live_path or 'unknown'}`\n"
        f"- verdict: `{verdict}` (driving task_type family: `{fam}`)\n"
        f"- {entry.get('reason', '')}\n\n"
        "## Evidence (first-pass-merge, applied vs baseline)\n\n"
        f"- applied:  {a_rate} (n={a_n}); Wilson 95% CI {a_ci}\n"
        f"- baseline: {b_rate} (n={b_n}); Wilson 95% CI {b_ci}\n"
        f"- delta (applied - baseline): {fv.get('first_pass_delta')}\n"
        f"- exploration deltas (applied - baseline; negative = explored less): "
        f"{fv.get('efficiency_delta')}\n"
        f"- judged at n ≥ {fv.get('min_sample')} with the Wilson CI — never on thin evidence.\n\n"
        "## What to do (human-gated)\n\n"
        + ("- **deprecate**: retire the skill (stop injecting it) — it is confidently\n"
           "  worse than not using it on this work.\n"
           if verdict == VERDICT_RETIRE else
           "- **revise**: the skill isn't helping — rewrite/tighten it, then re-measure;\n"
           "  it is not confidently harmful, so a tune is preferred over retirement.\n")
        + "- NEVER auto-applied: a human edits/retires the live skill; this role never does.\n"
    )


# ===========================================================================
# DB-integrated role — judge live skills → propose (reviewable) for underperformers
# ===========================================================================


def _write_proposal(
    conn: Any,
    task: Task,
    content: str,
    path: str,
    *,
    tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig],
    sink: EventSink,
    invoke_fn: Callable[..., Any],
) -> tuple[str, Optional[str]]:
    """Write the reviewable proposal via the policy-gated filesystem tool.

    Returns ``(status, path)``; a role without ``fs.write`` is DENIED (nothing
    written) — a safe, logged no-op, mirroring :func:`runtime.roles.curator`.
    """
    result = invoke_fn(
        role=ROLE,
        tool_name="filesystem",
        registry=tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=path,
        content=content,
    )
    status = getattr(result.status, "value", str(result.status))
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    return status, (path if wrote else None)


def _live_skill_paths(skills: Optional[SkillRegistry]) -> dict[str, Optional[str]]:
    """The LIVE (``reviewed:true``) skill names → their source path (or ``None``).

    Only reviewed skills are considered live; an unreviewed/candidate skill is never
    judged (it isn't an adopted capability). Empty when no registry is available.
    """
    if skills is None:
        return {}
    return {s.name: s.path for s in skills.all() if s.reviewed}


def run_skill_lifecycle(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    tool_registry: Optional[ToolRegistry] = None,
    policy: Optional[PolicyConfig] = None,
    skills: Optional[SkillRegistry] = None,
    min_sample: int = MIN_TRUSTWORTHY_SAMPLE,
    efficiency_floor: float = DEFAULT_EFFICIENCY_FLOOR,
    proposals_dir: str = DEFAULT_PROPOSALS_DIR,
    efficacy_report: Callable[..., dict] = _skill_efficacy_report,
    compute_verdicts: Callable[..., dict] = _skill_lifecycle_verdicts,
    invoke_fn: Callable[..., Any] = invoke,
) -> SkillLifecycleResult:
    """Service one skill-lifecycle task: judge LIVE skills → propose for underperformers.

    Builds the P1 :func:`runtime.quality.skill_efficacy_report` for ``task.workstream``,
    computes per-live-skill keep/tune/retire verdicts
    (:func:`runtime.quality.skill_lifecycle_verdicts`, restricted to the reviewed
    skills in ``skills``), and for each ``retire``/``revise`` skill (bounded to
    :data:`MAX_PROPOSALS`): writes a REVIEWABLE deprecation/revision proposal via the
    policy-gated filesystem tool and emits a body-free
    ``skill.deprecation_proposed`` / ``skill.revision_proposed`` event.

    It NEVER auto-retires, NEVER edits/removes a live skill file, and enqueues NOTHING
    (no ``enqueue`` seam is threaded here) — so a lifecycle task cannot spawn another
    (no loop). ``min_sample`` / ``efficiency_floor`` may be overridden from
    ``task.payload``. Injectable seams (``efficacy_report`` / ``compute_verdicts`` /
    ``invoke_fn``) keep it testable; ``policy`` gates the proposal write. Keyless-safe:
    with no ``skills`` registry no live skill is known → nothing is judged/proposed.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    min_sample = int(payload.get("min_sample", min_sample))
    efficiency_floor = float(payload.get("efficiency_floor", efficiency_floor))

    live_paths = _live_skill_paths(skills)
    report = efficacy_report(conn, task.workstream)
    verdicts = compute_verdicts(
        report,
        # ALWAYS pass a concrete live set (never None): an empty/None registry yields
        # an empty set → NOTHING is judged. This upholds the "only LIVE (reviewed)
        # skills are judged" invariant even by direct API — a candidate/unknown applied
        # skill is never judged (never proposed for deprecation/revision).
        live_skills=set(live_paths),
        min_sample=min_sample,
        efficiency_floor=efficiency_floor,
    )

    # Underperformers first, strongest-concern (retire before revise) then name — so
    # the MAX_PROPOSALS cap keeps the most concerning proposals.
    underperformers = [
        e for e in verdicts["by_skill"] if e["verdict"] in _ACTIONS
    ]
    underperformers.sort(
        key=lambda e: (0 if e["verdict"] == VERDICT_RETIRE else 1, e["skill"])
    )

    proposals: list[ProposedSkillAction] = []
    for entry in underperformers[:MAX_PROPOSALS]:
        name = entry["skill"]
        verdict = entry["verdict"]
        action, event_type = _ACTIONS[verdict]
        live_path = live_paths.get(name)
        fam = entry.get("driving_family")
        fv = next((f for f in entry.get("by_family", [])
                   if f["task_family"] == fam), None) or {}

        # 1. Write the reviewable proposal via the policy-gated tool (never the live
        #    skills/ root). Denied cleanly without fs.write — a safe no-op.
        proposal_status = "off"
        proposal_path: Optional[str] = None
        if tool_registry is not None:
            content = render_proposal(entry, action, live_path=live_path)
            proposal_status, proposal_path = _write_proposal(
                conn, task, content, f"{proposals_dir}/{_slug(name)}.{action}.md",
                tool_registry=tool_registry, policy=policy, sink=sink, invoke_fn=invoke_fn,
            )

        # 2. Emit the body-free proposal event (name + verdict + evidence only).
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=event_type,
                task_id=task.id,
                payload={
                    "skill": name,
                    "verdict": verdict,
                    "action": action,
                    "task_family": fam,
                    "applied_first_pass_rate": fv.get("applied_first_pass_rate"),
                    "baseline_first_pass_rate": fv.get("baseline_first_pass_rate"),
                    "first_pass_delta": fv.get("first_pass_delta"),
                    "applied_ci95": (list(fv["applied_first_pass_ci95"])
                                     if fv.get("applied_first_pass_ci95") else None),
                    "applied_n": fv.get("applied_n"),
                    "baseline_n": fv.get("baseline_n"),
                    "efficiency_delta": fv.get("efficiency_delta"),
                    "min_sample": min_sample,
                    "efficiency_floor": efficiency_floor,
                    "proposal_written": bool(proposal_path),
                    "reviewed": PROPOSAL_REVIEWED,   # invariant: never adopted here
                    "auto_retired": False,           # invariant: never auto-retired
                },
            )
        )

        proposals.append(
            ProposedSkillAction(
                skill=name,
                verdict=verdict,
                action=action,
                driving_family=fam,
                reason=entry.get("reason", ""),
                applied_first_pass_rate=fv.get("applied_first_pass_rate"),
                baseline_first_pass_rate=fv.get("baseline_first_pass_rate"),
                first_pass_delta=fv.get("first_pass_delta"),
                applied_ci95=(tuple(fv["applied_first_pass_ci95"])
                              if fv.get("applied_first_pass_ci95") else None),
                applied_n=fv.get("applied_n"),
                baseline_n=fv.get("baseline_n"),
                efficiency_delta=fv.get("efficiency_delta", {}),
                live_skill_path=live_path,
                proposal_status=proposal_status,
                proposal_path=proposal_path,
            )
        )

    return SkillLifecycleResult(
        workstream=task.workstream,
        skills_judged=verdicts["skills_judged"],
        verdicts=verdicts["verdicts"],
        proposals_made=len(proposals),
        proposals=proposals,
        min_sample=min_sample,
        efficiency_floor=efficiency_floor,
    )
