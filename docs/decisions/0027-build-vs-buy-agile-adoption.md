# 0027 — Build vs. buy/borrow + agile adoption: a PM operating principle, not a cron

- **Status:** Accepted
- **Date:** 2026-07-26

## Context

The studio must not fall behind. The stakeholder directive: *always have a
research agent (slow cadence, ~daily/weekly) learning the latest industrial
developments; don't hard-code it — internalize it as a higher-level principle the
PM incorporates; make cadence adjustable by token budget (etc.); but the idea
stays — hedge **build vs. buy/borrow** and stay agile/flexible about adopting a
better paradigm/tech — **without churning/looping.***

We already have most of the machinery:

- The **Researcher** ([ADR-0003](0003-workstream-operating-model.md),
  `runtime/roles/researcher.py`) mines external best-practice via the policy-gated
  cached search gateway and distills recallable lessons; it drafts only
  `reviewed: false` candidates and **enqueues nothing** (no research-of-research
  loop).
- The **adaptive** module (`runtime/adaptive.py`, ADR-0003) already scales
  review/retro/research **eagerness** per workstream from FACTS (recent error rate,
  activity) throttled by budget headroom — a deterministic, bounded, pure decision
  core.
- The **PM** (`runtime/roles/pm.py`, ADR-0003) is the only planning role; it wakes
  on a `pm.tick` pulse, plans, and enqueues via the queue only (no agent-to-agent
  calls, CLAUDE.md invariants 1–2).
- Model/tooling adoption is already review-gated ([ADR-0005](0005-model-registry-router.md)
  sourcing; [ADR-0008](0008-adopt-agent-skills-standard.md) skills are reviewed
  before use); the Sourcing/Curator/Failure-analyst roles all *propose*, never
  auto-adopt.

What was missing was the **principle that ties this together**: a durable
disposition the PM carries — keep learning from outside, and continuously weigh
building in-house against buying/borrowing and adopting a better paradigm — rather
than a one-off or a rigid scheduled job.

## Decision

Internalize the directive as a **PM operating principle**, realized minimally by
reusing what exists. Two additive, behavior-preserving pieces:

1. **Encode the principle in the PM operating prompt.** `compose_role_prompt`
   (`runtime/roles/prompt.py`) gains an opt-in `strategy_aware` layer (mirroring the
   existing `budget_aware` layer): a fixed, bounded section instructing the role, on
   every plan / retro / major decision, to (a) prefer adopting a mature
   component/service/standard over reinventing it — build in-house only when clearly
   justified; (b) stay flexible about a materially better paradigm/framework/tool;
   (c) **change only on clear evidence — no churn** (don't thrash between options or
   chase novelty); and (d) surface any build-vs-buy / paradigm change as a
   **reviewable proposal** — never silently self-adopt. The PM opts in
   (`strategy_aware=True`); the layer is off by default so all other prompt
   assembly is byte-identical.

2. **A budget-tuned baseline external-research cadence the PM owns.**
   `runtime.adaptive.pm_research_interval_hours(budget_remaining)` is a **pure,
   deterministic** function returning the interval (in hours) between studio-level
   external scans: FASTEST (`research_baseline_min_hours`, ~daily) with ample budget
   headroom, SLOWEST (`research_baseline_max_hours`, ~weekly) when starved — bounded
   to that closed range and **never zero / never off**. This is distinct from the
   per-workstream *episode* eagerness (`research_cadence`, which may be `off`): the
   studio-level scan is always at least minimal. It is independent of the
   `ADAPTIVE_INTENSITY` master switch — the baseline is a PM principle, not an
   opt-in escalation.

   On each `pm.tick`, `run_pm_tick` checks the cadence and, when a scan is **due**,
   commissions **exactly ONE** bounded `research` task (goal: *scan the latest
   industrial developments relevant to the studio; propose reviewable candidates;
   weigh build vs. buy/borrow; do not auto-adopt*). The worker's existing
   `research` dispatch runs it via `run_research`. **The PM owns the trigger; there
   is no cron/launchd job.**

### Dueness and the no-churn / no-loop guardrails

- **At most one scan per due-window (idempotent — never stack).** "Due" = a full
  cadence interval has elapsed since the last `research` task (counting pending OR
  finished tasks, so a scan already in flight suppresses a new one). If the
  workstream has never been scanned, it becomes due only after it has been *active*
  for a full interval (a warm-up, so a brand-new workstream isn't scanned on its
  very first pulse). Dueness is derived from the tasks/event history — **no new
  schema.**
- **No research-of-research loop.** A `research` task enqueues nothing (enforced in
  the Researcher and the worker's `research` handler); only the PM's pulse
  commissions scans.
- **Never auto-adopt.** Findings are `reviewed: false` proposals / recallable
  lessons only. Acting on them — an actual build-vs-buy or paradigm change — stays
  a deliberate, evidence-gated decision surfaced for review (ADR-0005 / ADR-0008).
  This ADR is **not** a rewrite engine and **not** a hard-coded schedule.
- **Degrade-safe.** Keyless/dry-run safe; if the DB is down or a budget read fails,
  the pulse is skipped and the `pm.tick` core (plan → gate → decompose) is never
  blocked or crashed ([ADR-0017](0017-db-resilience-and-remote-access.md)).

## Consequences

- The studio continuously scans external developments at a slow, budget-aware
  cadence the PM modulates — faster with headroom, slower (never off) when starved
  — with no rigid job and no churn.
- Every planning/retro/decision prompt carries the build-vs-buy + agile-adoption
  disposition, nudging the studio to assemble mature components (CLAUDE.md) and
  stay open to better paradigms while changing only on evidence.
- Fully additive: with the new prompt layer off by default and the commission
  degrade-safe + idempotent, existing behavior and tests are preserved.

## Cross-references

- [ADR-0003](0003-workstream-operating-model.md) — adaptive, evidence-based cycle;
  the Researcher; "more research in fast-moving domains, throttled by budget".
- [ADR-0005](0005-model-registry-router.md) — model sourcing proposes,
  human-gated adoption (the build-vs-buy envelope for models).
- [ADR-0008](0008-adopt-agent-skills-standard.md) — skills reviewed before use;
  never auto-adopt an unreviewed change.
- [ADR-0024](0024-skill-induction.md) — measure-first, review-gated induction of
  reusable know-how (the same propose-not-adopt discipline).
