# 0031 — Role-agnostic worker dispatch + PM commissions all roles

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

The worker's `run_once` dispatched a claimed task to its role by a hand-written
`if task.type == … / elif task.type in …` chain — one bespoke branch per role.
Two problems followed from that shape:

1. **Roles could have a handler but never run.** Several roles
   (Sourcing, Failure-pattern analyst, Skill-lifecycle) had a worker branch AND a
   task-type constant but **no live producer** — nothing ever enqueued their type,
   so they were dormant. The Capacity Steward was worse: it had a callable
   (`run_capacity_steward`) but **no task type and no dispatcher at all**, so it
   could only be called from tests.
2. **Adding/wiring a role meant editing the dispatch chain**, and the chain's
   ordering carried a load-bearing subtlety (the coding types `work.code` /
   `prototype` had to be matched *before* the generic `work.` prefix) that a new
   branch could silently break.

Stakeholder direction (2026-07-27): *"the roles need to all be exposed to PM, and
PM needs the ability to enqueue tasks for these roles. Ideally the agent dispatcher
should be compatible to all roles, not that we have one dispatcher for one specific
type of tasks."* — i.e. fix the architecture, not wire one role.

## Decision

**1. Role-agnostic dispatch registry.** A single `task_type → handler` registry
(`runtime.worker._EXACT_HANDLERS` + the `work.` prefix rule) replaces the if/elif
chain. `resolve_handler(task_type)` resolves in the exact-same order the chain did:

- exact `task_type` hit first — this includes the coding types `work.code` /
  `prototype`, so the loop-free coding path still wins over the generic work loop
  (architecture §14, the specific-before-prefix invariant);
- else a `work.`-prefixed type → the unified Executor→Verifier dev/review loop;
- else unknown → the explicit *abandoned* fallback (never a silent drop).

Each entry is a thin adapter over the existing `_handle_*` function, called with the
same arguments via a shared `DispatchContext` (the task + every injectable seam + the
resolved workstream config + orchestration modes). Behavior per existing type is
byte-identical; adding a role is now a one-line registry row keyed off the role's own
task-type constant — the single source of truth for "what the worker can run".

**2. Dormant roles are registered.** Sourcing / Failure-analyst / Skill-lifecycle are
in the registry (as before). The Capacity Steward gets a task type
(`capacity.review`, `runtime.roles.capacity_steward.CAPACITY_REVIEW_TYPE`) and a
handler that wraps its lifecycle-free entrypoint minimally (heartbeat → run → commit
`MERGED`, via the guarded completer), exactly like the other read-only proposer roles.
No role's internal logic was rewritten.

**3. The PM commissions all roles.** The PM is the producer:

- `runtime.roles.pm.PM_ROLE_TASK_TYPES` is the catalog of specialist role task types
  the PM may enqueue, each with a one-line "when to use it" note.
- `role_catalog_note()` injects that menu into the PM plan prompt (via a new opt-in
  `role_catalog=` section of `compose_role_prompt`), so the planner **knows the roles
  exist**.
- `enqueue_role_task(...)` is the bounded helper the PM planner calls to drop ONE
  `up_for_grabs` task for a role (validated against the catalog; unknown type raises
  rather than enqueuing a task the worker would only abandon). A consistency test
  guarantees every catalog type resolves to a live handler.

These roles are **NOT** put on autonomous crons: the PM enqueues them **by judgment**.
The workstream is not self-sufficient yet — the human/PM stays in the loop.

**4. The Critic stays an in-process consult, not a queue task.** The Critic (ADR-0019)
critiques the PM's *draft plan* inside `run_pm_tick`, in a bounded loop, **before** the
PM decomposes/enqueues. A queue task cannot express that seam: there is no plan to
critique until the PM is mid-tick, and routing it through the queue would break the
"consult before commit" ordering and add a round-trip with nothing to act on. So the
Critic is deliberately excluded from the role registry and remains the in-process
`critic=` seam of `run_pm_tick`. (Its consult *is* still observable/replayable — it
records a trajectory step and emits `pm.consensus`.) This is the one role that is a
seam rather than a dispatched task, by design.

## Consequences

- Dispatch is data-driven and role-agnostic: a regression that drops or misroutes a
  type fails the mapping-table test loudly; the specific-before-prefix invariant is
  asserted directly (`work.code` → coding path, not the work loop).
- The four previously-dormant roles can now actually run when the PM commissions them
  — closing the "handler but no producer" gap. Capacity Steward is now dispatchable at
  all (it had no task type before).
- Adding a PM-commissionable role = add its task-type constant, a registry row, and a
  `PM_ROLE_TASK_TYPES` entry. No new dispatch branch, no ordering surgery.
- No new autonomy: nothing schedules these roles; the PM's judgment (and later a human)
  gates every commission. Every dispatched role still proposes reviewable output only
  and enqueues nothing itself (no loops), preserving the existing bounded-coordination
  invariants (CLAUDE.md invariant 1, ADR-0015).
