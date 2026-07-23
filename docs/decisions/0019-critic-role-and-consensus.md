# 0019 — Critic role & the PM↔Critic consensus loop

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

The studio already has an independent guard on **completed work**: the
**Reviewer / Whistle-blower** ([ADR-0003](0003-workstream-operating-model.md),
[ADR-0014](0014-validation-rigor.md)) scans a *finished* episode for risk/disaster
signals it can observe from evidence. That is an **after-the-fact** check.

Nothing, however, adversarially challenges a **decision before the studio commits
to it**. The PM produces a plan, self-scores its confidence, and — if confident —
decomposes and enqueues. The PM's confidence gate guards against *executing what it
does not understand*, but a PM that is confidently wrong sails straight through it.
The literature ADR-0003 already cites (Reflexion / dual-role critic, multi-agent
surveys) names an adversarial **critic** partner as a distinct, high-leverage role:
one whose job is to *disagree productively* — surface risks, downsides, missed
opportunities, and alternatives — on forward-looking decisions (ideation/planning,
retro, major choices).

Crucially this is **not** the Reviewer. The Reviewer asks "did this finished work
go wrong?"; the Critic asks "is this decision we are about to make wrong or
incomplete?". Conflating them would either weaken the evidence-guard (by making it
speculative) or leave planning unchallenged.

## Decision

Add a **Critic** role and a bounded **PM↔Critic consensus loop**.

- **The Critic (`runtime/roles/critic.py`)** is a forward-looking adversarial
  partner. Given a proposed decision + its **structured facts**, `run_critic`
  returns a `Critique`: a list of `Concern`s each classified `kind ∈ {risk,
  downside, missed_opportunity, alternative}` with a `severity`, plus a `blocking`
  flag and a `recommendation ∈ {proceed, revise, escalate}`. Its job is to try to
  find what is **wrong or missing**, not to agree.
- **Evidence, not vibes** ([ADR-0014](0014-validation-rigor.md)). Like every other
  validator, the Critic's verdict is computed **deterministically from the facts of
  the subject** (`assess_concerns`), so it is reproducible and testable keyless. It
  makes a traceability-only `call_model(role="critic", task_type="critique")`
  dry-run call (routed/costed/logged like any model call), but that call's text
  does **not** decide anything — a lying "looks great" model changes nothing.
- **The consensus loop** (ADR-0003's operating pattern, made concrete):
  **PM proposes → Critic critiques → PM drives to consensus (revise or justify) →
  converge, or escalate a genuine disagreement to the stakeholder (🛑).** In the
  PM's confidence gate, after a feasible + confident `Plan` is produced and **before**
  decompose/enqueue, the PM consults the Critic. A non-blocking critique → proceed;
  a blocking one → the PM revises and re-consults; an `escalate` (or the round bound
  reached while still blocked) → a first-class 🛑 `pm.pushback` to the stakeholder,
  enqueuing no work.
- **Bounded — never a loop.** The consult↔revise cycle is capped by
  `PM_CRITIC_ROUNDS` (default 2). This is a hard bound (the #1 multi-agent failure
  is handoff loops, ADR-0003); the last allowed round, if still blocked, escalates
  rather than iterating.
- **Opt-in + behavior-preserving.** The Critic is wired by passing a `critic=`
  callable to `run_pm_tick` / `run_retro`. With none (the default), the consult is
  skipped and the PM/Retro behave exactly as before. The **Retro** consult is a
  single, bounded, **advisory** challenge of the distilled lessons before they are
  stored (a retro is not a human-gated commitment).
- **Observability without leakage** (invariants 5 & 6). The Critic emits
  `critic.reviewed` and the loop emits `pm.consensus`, both carrying **only** counts
  / kinds / severities / recommendation / rounds / outcome — never a plan body,
  lesson text, concern prose, or secret.

## Consequences

- Forward decisions now have an adversarial check the way finished work has the
  Reviewer — the two are deliberately distinct roles with distinct events
  (`critic.reviewed` vs `review.flagged`), timing (before vs after commitment), and
  effect (drives consensus/escalation vs raises an after-the-fact alarm).
- A confidently-wrong plan is caught before work is enqueued, and a genuine
  PM↔Critic disagreement reaches the stakeholder as a 🛑 rather than being silently
  resolved by either side.
- The consult adds a bounded number of (dry-run, cheap) model calls per plan; it is
  opt-in, so cost is only paid where a workstream wires it, and is scaled by the
  same adaptive-intensity philosophy as review/retro (ADR-0003).
- Wiring the Critic through the **worker** is deferred (this change does not touch
  `worker.py`); the seam + the demo prove the contract, and a later change can wire
  it via workstream config the same way overlays/checkers are wired (ADR-0018).

## References

- [ADR-0003](0003-workstream-operating-model.md) — roles = prompt+skills+tools; PM
  owns completion + may push back; dual-role critic / Reflexion; handoff-loop
  failure mode; adaptive intensity.
- [ADR-0014](0014-validation-rigor.md) — validators trust evidence, not claims.
- [ADR-0006](0006-stakeholder-comms.md) — 🛑 "approve (blocks)" stakeholder escalation.
