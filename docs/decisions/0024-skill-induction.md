# 0024 — Skill induction: measure-first efficacy, then a review-gated Curator

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

A role is `prompt + skills + tools` ([ADR-0008](0008-adopt-agent-skills-standard.md)).
Today every skill in `skills/` is **human-authored** and hand-reviewed
(`define-success-criteria`, `code-review`, `rigorous-review`, `retrospective`),
selected on-demand by `SkillRegistry.select` and injected — reviewed-only by
default — via `runtime.skills.inject.compose`. The learning loop that *creates*
new know-how is split today: the **Retro** role distills lessons from our own
finished work ([ADR-0003](0003-workstream-operating-model.md)) and the
**Researcher** brings in external practice; both currently land as *lessons*
(prompt text), not as durable, reusable *skills*.

We want the studio to **induce its own skills** — notice that it keeps solving a
recurring class of task the same effective way, capture that routine as a skill,
and reuse it — without violating the invariants (agents don't self-modify live
config; skills are code and reviewed before use; everything observable/replayable;
no unbounded context growth). The risk of getting this wrong is well known: a
system that auto-admits self-generated routines with no verification accumulates
plausible-but-useless cruft that *raises* cost.

The literature converges on a small set of load-bearing principles (established;
we cite them as the design basis, and flag where a claim still needs primary
confirmation before we lean on it hard):

- **Voyager** (Wang et al., 2023) — grows a **skill library** of verified,
  reusable code routines during open-ended play; a routine is admitted **only
  after it self-verifies** against a concrete success check, then is retrieved by
  similarity later. *Principle: verify-before-admit; retrieve by relevance.*
- **Agent Workflow Memory / AWM** (Wang et al., 2024) — **induces reusable
  workflows (routines) from successful trajectories** and reuses them, improving
  success and reducing steps on later tasks. *Principle: induce routines from your
  own successful trajectories; the payoff is fewer steps.* (Effect sizes are
  benchmark-specific — treat "reduces steps" as directional, to be re-measured on
  our own traffic, not a guaranteed number.)
- **Anthropic Agent Skills** (2025) — skills are **human-authored, reviewed,
  composable** capability packages with **progressive disclosure** (load a short
  descriptor; pull the full body/resources only when actually used). *Principle:
  the artifact format + progressive disclosure; a human stays in the loop.*
- **DreamCoder** (Ellis et al., 2021) — grows a library by an
  **MDL / "earns-its-place"** criterion: an abstraction is kept only if it
  compresses the solution set (pays for its own description length). *Principle: a
  new skill must earn its keep or be retired.*
- **DSPy / LATM** (Khattab et al., 2023; Cai et al., 2023) — **verify a
  generated tool/routine on held-out cases before admitting it** to the reusable
  set. *Principle: shadow-evaluate before you trust.*

None of these ship a solo-studio-grade, event-sourced, policy-gated version. This
ADR designs that, and — critically — **sequences it measure-first**: we do not
induce a single skill until we can *measure a skill's efficacy* on the
human-authored skills we already run. **P0 + P1 land now; P2–P5 are future work
scoped here so the phases share one design.**

## Decision

Build skill induction as a **staged pipeline** on the existing substrate (event
log, trajectories, telemetry rollups, the reviewed-skill injection gate). Two
non-negotiables carried from the invariants:

1. **Verify/measure before admit.** A candidate skill is never trusted because it
   was generated; it earns its place on *evidence* (Voyager/DSPy/DreamCoder).
2. **Curator proposes, never mutates.** Induction writes a `reviewed:false`
   candidate through the policy-gated filesystem tool and stops — exactly the
   **Sourcing** propose→review pattern ([ADR-0008](0008-adopt-agent-skills-standard.md)
   §"review before use"). Live `skills/` is only ever changed by a human merge, or
   by the narrow auto-adopt lane below (its own ADR, strict conditions).

### Phases

- **P0 — Attribution (NOW).** Emit a **body-free `skill.applied`** event whenever a
  *reviewed* skill is injected into a prompt (`runtime.skills.inject.emit_skill_applied`,
  wired into pm / executor / verifier / reviewer / critic). Payload = injected
  skill **name(s) + role** only; `task_id`/`workstream` on the envelope; **never a
  skill body** (invariants 5 & 6, mirroring `trajectory.*` / `model.call.failed`).
  This is the missing telemetry that makes per-skill effect *attributable*.
- **P1 — Efficacy measurement (NOW).** `runtime.quality.skill_efficacy_report`:
  for each applied skill, an **applied-cohort vs baseline** comparison on the
  efficiency metrics below, pooled across similar task_types. Read-only, additive
  to `quality_report` (`skill_efficacy`). **Measures only** — no induction, no
  proposal, no Curator.
- **P2 — Skill Curator (FUTURE, own review).** A learning role that scans **mature
  reasoning trajectories** ([ADR-0020](0020-trajectory-observability.md)) for a
  **recurring + successful + efficient** cluster (AWM-style routine induction),
  drafts a `SKILL.md`, and **proposes it as a `reviewed:false` candidate** via the
  fs tool (Sourcing pattern). It never mutates a live skill and never self-approves.
- **P3 — Dual-source convergence (FUTURE).** Fold the Retro (internal) and
  Researcher (external) streams into the Curator so an induced-from-our-trajectories
  skill and an external-best-practice skill land in the **same candidate pipeline**
  and are judged by the **same efficacy bar** (P1), rather than living as ad-hoc
  lessons.
- **P4 — Keep / tune / retire (FUTURE).** A DreamCoder-style "earns-its-place"
  sweep: a skill whose applied cohort shows **no CI-separated gain** over baseline
  is flagged for retirement or revision — the library is pruned, not just grown.
- **P5 — Hierarchy (FUTURE, DEFERRED).** **Defer skill-trees / skill-composition
  hierarchies.** Adopt **intra-skill progressive disclosure** now instead (a skill
  exposes a short descriptor and pulls its full body/resources only when used) —
  it captures most of the context-cost win of hierarchy ([ADR-0013](0013-context-management.md))
  at a fraction of the complexity, and matches the Agent Skills format.

### Efficacy loop — how a skill is measured (P1, the bar every phase reuses)

Efficacy = **reduced trial-and-error / exploration** to reach the same or better
outcome. For an applied skill we compare its **applied cohort** (tasks where
`skill.applied` names it) against a **baseline cohort** (comparable tasks of the
same task_type *without* it), on:

- **iterations** — trajectory steps per task (exploration proxy; lower better);
- **input tokens per outcome** — summed `model.call` input tokens per task;
- **tool + search calls** — `tool.invoked` + `search.provider_call` per task;
- **first-pass-merge rate** — merged with no reviewer round-trip;
- **verify-pass rate** — the independent evidence gate passed.

Count metrics carry **mean + n + `insufficient_sample`**; rate metrics carry
**n + Wilson 95% CI + `insufficient_sample`** (reusing `_rate_ci` / the
`MIN_TRUSTWORTHY_SAMPLE=30` doctrine, [ADR-0012](0012-telemetry-metrics.md) /
[ADR-0014](0014-validation-rigor.md)). A `1.0` on `n=3` is **flagged, never
trusted** — the same anti-fabrication rigor as everywhere else.

**Pooling across similar task_types.** A solo studio will not hit `n≥30` per
`(skill, exact task_type)` quickly. We therefore pool comparable task_types into a
**family** (the leading `.`-delimited segment of `tasks.type`: `work.a` / `work.b`
→ `work`) and draw the applied-vs-baseline A/B **within one family**. Pooling is
legitimate **only because usage is genuinely per-skill attributed** (P0's
`skill.applied`) and the cohorts are comparable work. This is a documented,
deterministic grouping, not an opaque cluster.

### Adoption (FUTURE) — review-gate by default, one narrow auto-adopt lane

- **Default: propose → human review.** Every candidate is `reviewed:false`; a human
  (or the review agent) approves the merge into `skills/`. No auto-mutation of live
  config ([ADR-0008](0008-adopt-agent-skills-standard.md), [ADR-0006](0006-stakeholder-comms.md)).
- **Narrow auto-adopt lane (its own ADR before it ships).** Only a candidate that
  is **doc-only** (instructions/text, no executable resources), passes a
  **shadow-eval** (measured on held-out/parallel traffic, DSPy/LATM style), and
  shows a **Wilson lower-bound gain at n≥30** on the P1 efficacy bar may auto-adopt
  — everything else stays on the review path. This lane is deliberately conservative
  and gated behind its own decision record.

## Consequences

- **Now:** per-skill usage is attributable (`skill.applied`) and we can measure a
  skill's efficacy against baseline on real traffic, with honest statistics — the
  prerequisite for ever inducing one. Both additions are body-free, event-sourced,
  read-only (P1), and behavior-preserving (no prompt bytes change; the emit is a
  no-op when nothing reviewed is injected).
- **Later:** the Curator can be added as a normal learning role with **no new
  mechanism** — it reuses trajectories (ADR-0020), the efficacy bar (P1), the
  Sourcing propose→review pattern, and the fs tool gate. Nothing about P2+ requires
  re-touching the injection path or the telemetry.
- **Guardrails preserved:** skills stay code (reviewed before use); induction
  proposes and never mutates live; the library is pruned by evidence (P4), not left
  to grow unboundedly; hierarchy complexity is deferred in favor of progressive
  disclosure.
- **Honest limits:** dry-run today means tokens/outcomes are the router's
  deterministic estimates; the efficacy numbers measure the **mechanism** now and
  become real-spend/quality signals once real models are wired (see
  `docs/evaluation.md`). The AWM/Voyager effect sizes are prior-art *direction*, to
  be re-confirmed on our own cohorts before any auto-adopt threshold is trusted.

## References

- ADR-0008 (Agent Skills standard; review-before-use; Sourcing propose→review),
  ADR-0003 (Retro/Researcher learning loop), ADR-0020 (reasoning trajectories),
  ADR-0012 (telemetry/metrics + Wilson CI), ADR-0014 (validation rigor),
  ADR-0013 (context management / progressive disclosure).
- Voyager (Wang et al., 2023); Agent Workflow Memory (Wang et al., 2024);
  Anthropic Agent Skills (2025); DreamCoder (Ellis et al., 2021);
  DSPy (Khattab et al., 2023) / LATM (Cai et al., 2023).
- Code: `runtime/skills/inject.py` (`emit_skill_applied`), `runtime/event_types.py`
  (`skill.applied`), `runtime/quality.py` (`skill_efficacy_report`),
  `runtime/tests/test_skill_applied.py`, `runtime/tests/test_skill_efficacy_db.py`.
