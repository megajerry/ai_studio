# 0020 — Trajectory observability (the reasoning trajectory as first-class data)

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

The runtime already persists two of the three layers of what the studio does:

- **Actions / State.** Every guarded task move writes a `task_transitions` row and
  emits a `task.transition` event; the append-only event log is the replayable
  record of *what happened*; per-call model telemetry (`model.call`, ADR-0012)
  records *what was spent*.

What it does **not** persist is the third layer: the ordered causal **trajectory**
of *how* an agent reached a decision — what it observed, the options it weighed,
what it decided and *why*, where the Critic pushed back (ADR-0019), what it
revised, and when it escalated.

This distinction matters most for the **PM**. The PM's decisions (decompose this
goal into these tasks; push back to the stakeholder; proceed despite a critique)
are the **highest-leverage, least-reversible** thing the studio does — an
executor's mistake is one task, a PM's mistake mis-shapes the whole plan. Today
that reasoning is invisible: we can see the tasks the PM enqueued, but not the
deliberation that produced them, so a confidently-wrong decision is
**unmeasurable** and un-learnable-from after the fact. Actions tell us *what*;
trajectories tell us *why* — and only the *why* can be graded against the outcome.

We therefore need trajectories to be first-class, replayable, and
**outcome-linkable** data, without breaking the existing invariants (body-free
event log; secrets/PII never on the wire or in git; observability + replay).

## Decision

Add a **trajectory persistence + writer foundation** (this change is T1: schema +
the single guarded writer; wiring the PM/Critic/roles to emit steps is a
follow-on, mirroring how the Critic seam was introduced ahead of worker wiring in
ADR-0019).

### Action vs. trajectory

An **action** is a committed state change (a task transition, a tool invocation).
A **trajectory** is the reasoning episode that *led to* actions: a bounded,
ordered, causal chain of reasoning `steps`. The two are complementary and are
joined by outcome attribution (below), not merged — actions stay in the event log
/ `task_transitions`; trajectories are their own tables.

### Schema (migration `0011_trajectories.sql`)

- **`trajectories`** — one bounded reasoning episode: `id`, `role`, `workstream`,
  `goal`, `status ∈ {open, closed}`, `retention_tier ∈ {verbatim, lean}` (default
  `verbatim`), `started_at`, `ended_at?`, `expires_at?` (TTL horizon),
  `context_size_start?` / `context_size_peak?` (ADR-0013 context growth),
  `tokens? / cost_usd? / latency_ms?` (roll-up), `outcome_summary?`, `created_at`.
- **`trajectory_steps`** — the ordered chain: `id`, `trajectory_id`, `seq`
  (monotonic **per trajectory**, gapless — the events `seq` pattern scoped to a
  parent), `step_type ∈ {observe, plan, decide, consult, revise, decompose,
  escalate, commit}`, `summary`, `rationale` (**full verbatim**),
  `options_considered` (jsonb), `choice?`, `confidence?`, `refs` (jsonb — task ids
  / event ids / critic verdicts), `context_size?`, `tokens? / cost_usd? /
  latency_ms?`, `created_at`.
- **Outcome attribution.** A nullable `tasks.trajectory_id` FK links a
  decomposition trajectory to the tasks it created, so a decision can be graded
  against how its tasks actually turned out (`... FROM tasks WHERE trajectory_id =
  $1`). It is `ON DELETE SET NULL` so a TTL expiry never orphans or blocks a task.
- Indexes: `UNIQUE(trajectory_id, seq)` (enforces gapless/no-dup ordering),
  `trajectories(role, workstream)`, `trajectories(expires_at)` (TTL sweeps),
  `tasks(trajectory_id)`.

### Single guarded writer

All writes to these tables go through **`runtime/trajectory.py`** — there are **no
ad-hoc INSERT/UPDATEs** elsewhere, exactly as `runtime.tasks.transition` is the
sole writer of task state (invariant 4) and `runtime.events` the sole event writer.
`add_step` assigns the next `seq` under a `FOR UPDATE` lock on the parent
trajectory row, so concurrent appends serialize with **no gaps or races** (the
`UNIQUE(trajectory_id, seq)` index backstops it). Every write accepts an injectable
`now` so the timeline is deterministic in tests.

### Event-sourced, replayable, and BODY-FREE

Each write emits a `trajectory.*` event (`trajectory.started`,
`trajectory.step_added`, `trajectory.closed`, `trajectory.compacted`,
`trajectory.expired`) in the **same transaction** as the row write, so the log is a
complete, replayable record (invariant 6). Crucially those events are **body-free**:
their payloads carry **only** ids / types / `seq` / `step_type` / tier / counts —
**never** the `goal`, `summary`, `rationale`, or `outcome_summary` text. The bodies
live in the **local DB only**, never on the wire and never in git — the same
discipline the event log already keeps for task/experiment free text (invariant 5,
ADR-0011; matches the `experiment.*` "no hypothesis text" rule).

### Retention: verbatim → lean, bounded by TTL

Per the stakeholder decision, the write path captures **full verbatim** traces —
fast and complete, with **no inline scrubbing** (scrubbing on the hot path would be
slow and lossy). Footprint is then bounded two ways:

1. **TTL.** `start_trajectory(ttl=…)` sets `expires_at`; `expire_trajectories(now)`
   is a global sweep that **hard-deletes** trajectories past their horizon
   (cascading their steps). Deletion — not marking — is deliberate: the point of
   the TTL is to reclaim the local footprint of verbatim bodies.
2. **verbatim → lean rotation.** A learning/Retro agent later calls
   `compact_to_lean(trajectory_id, distill_fn=…)`, which replaces each step's
   verbatim `rationale` with a distilled form and flips `retention_tier` to `lean`.
   The rotation is **lossless on outcome-relevant facts**: `choice`, `confidence`,
   `refs`, and the trajectory's `outcome_summary` are preserved untouched — only the
   long free-text `rationale` body is distilled (default: first line, truncated).

## Consequences

- The PM's (and any role's) reasoning becomes first-class, replayable data that can
  be graded against outcomes via the `tasks.trajectory_id` join — a
  confidently-wrong decision is now *measurable*, closing the loop for the
  learning/Retro agents.
- The event log stays body-free and PII/secret-free (public-repo safe), while rich
  bodies live locally under a bounded footprint (TTL + lean rotation).
- A new persistence surface adds a migration and a writer module; it is inert until
  roles are wired to emit steps (deferred, like the Critic worker wiring in
  ADR-0019), so this change carries no behavioral risk to the running loop.
- The single-guarded-writer rule keeps `seq` gapless and the events consistent, at
  the cost that all trajectory writes must import `runtime.trajectory` (intended).

## References

- [ADR-0012](0012-telemetry-metrics.md) — append-only telemetry (`task_transitions`,
  `model.call`); trajectories are the *reasoning* layer above these action metrics.
- [ADR-0013](0013-context-management.md) — context size dominates cost; trajectories
  record `context_size_start/peak` and are footprint-bounded (TTL + lean rotation).
- [ADR-0014](0014-validation-rigor.md) — evidence over claims; the outcome-attribution
  join lets a decision be judged on how its tasks actually turned out, not its self-report.
- [ADR-0015](0015-task-lifecycle-state-machine.md) — the single-guarded-writer +
  gapless-`seq` discipline this writer mirrors (`runtime.tasks.transition`).
- [ADR-0019](0019-critic-role-and-consensus.md) — the PM↔Critic loop whose consult /
  revise / escalate turns are exactly the `consult` / `revise` / `escalate` steps a
  PM trajectory records; body-free `critic.reviewed` / `pm.consensus` set the pattern.
