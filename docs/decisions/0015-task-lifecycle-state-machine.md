# 0015 — Canonical task-lifecycle state machine

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Tasks had five ad-hoc statuses (`queued | in_progress | blocked | done | failed`)
written from several places (`tasks.py`, `worker.py`, `supervisor.py`), each with
its own guarded UPDATE. There was no single definition of the legal lifecycle, no
first-class notion of the dev/review flow (submit → review → approve → merge), no
way to know which tasks are parallelizable vs blocked by a prerequisite, and only
coarse telemetry (a `task.finished` event). As the fleet grows (PM, Executors,
Verifier, human/off-host Reviewer) we need one canonical, guarded, observable
lifecycle that every agent shares — knowable even when the DB is down.

## Decision

**One canonical state machine, one guarded transition, no ad-hoc status writes.**

### States (`runtime/task_state.py`)

`up_for_grabs, claimed, in_progress, blocked, ready_for_review, reviewer_blocked,
approved, merged, abandoned`. Terminal: **`merged`** (success) and **`abandoned`**
(dropped). Legacy mapping: `queued→up_for_grabs`, `done→merged`, `failed→abandoned`
(`in_progress`/`blocked` unchanged).

### Legal transitions

The forward lifecycle (a `TRANSITIONS: dict[str, set[str]]` with
`can_transition` / `assert_transition`):

```
up_for_grabs     → claimed, abandoned
claimed          → in_progress, up_for_grabs, abandoned
in_progress      → ready_for_review, blocked, abandoned
blocked          → in_progress, abandoned
ready_for_review → approved, reviewer_blocked, abandoned
reviewer_blocked → in_progress, abandoned
approved         → merged, abandoned
merged / abandoned → (terminal)
```

Plus two documented **operational recovery edges** (the liveness layer, ADR-0004 —
not part of the forward flow): `in_progress → up_for_grabs` (supervisor re-kick of
a stale worker) and `blocked → up_for_grabs` (re-queue once a 🔴 approval is
granted, so a fresh worker re-runs the action and finds the grant).

### The one guarded transition

Every state change goes through `runtime.tasks.transition(conn, task_id, to, …)`,
which in one transaction: verifies legality (illegal → `IllegalTransition`), does
the UPDATE guarded on the current status (a concurrent change is a no-op), records
lifecycle telemetry, and emits a `task.transition` event. **No status is UPDATEd
anywhere else.** `grab_task` / `claim_task` / `start_task` / `complete_task` /
`block_task` / `requeue_blocked_task` / `rekick_task` are all thin wrappers over it.

### Verifier as the automated Reviewer (unified runtime loop)

The runtime loop and the dev/review flow are the same path. In one `run_once`:
grab (`up_for_grabs → claimed`) → start (`→ in_progress`) → Executor → submit
(`→ ready_for_review`) → **the Verifier is the automated reviewer**: pass
(`→ approved → merged`); fail (`→ reviewer_blocked`), retry (`→ in_progress`)
while attempts remain, else abandon. A 🔴 approval pend parks the task `blocked`;
once granted it is re-queued and re-driven. A **human / off-host Reviewer** can
perform `ready_for_review → approved | reviewer_blocked` by querying
`list_for_review`. Internal tasks (pm.tick / retro / research / review) are
auto-approved through the same states via `complete_task`.

### Task dependencies (the DAG)

Tasks carry `depends_on uuid[]` (prerequisite task ids). A task is **grabbable
only when `up_for_grabs` AND every prerequisite is `merged`** — `grab_task`
filters out any task with an unmet prerequisite (`NOT EXISTS (a prereq whose
status <> 'merged')`). Effect: tasks with no unmet deps are independent and
grabbable in parallel; dependents wait. A prerequisite that is `abandoned` means
the dependent can never run — it is surfaced by `waiting_tasks` (never silently
grabbed). `ready_tasks` lists what is grabbable now (parallelizable); the PM sets
the edges when it decomposes a goal, and cycles / self-dependencies are rejected
(`DependencyCycle`).

### Lifecycle telemetry (extends ADR-0012)

An append-only `task_transitions` table (`task_id, from_status, to_status,
agent_id, agent_type, at, latency_ms`) is written by `transition()`, with
`latency_ms` = time since the task's previous transition. Every model call on a
task's behalf already carries `task_id`, so cost/tokens link per task. Query
helpers: `task_lifecycle` (ordered transitions + durations + total wall-clock),
`task_cost` (tokens + cost + latency from that task's `model.call` events), and
`agent_rollup` / `model_rollup`. Events (`task.transition`) carry ids / statuses /
agent / latency only — never secret text (invariants 5 & 6).

## Consequences

- One place to reason about — and change — the lifecycle; illegal moves are
  impossible, not merely discouraged.
- The fleet knows what is parallelizable (independent) vs waiting (dependent), so
  a spawner can fan out safely.
- Rich per-task / per-agent / per-model telemetry falls out of the guard for free.
- Migration `0008_task_lifecycle.sql` is forward-only + idempotent and maps legacy
  statuses in place.
- **Follow-up (separate):** DB-outage resilience and remote host-restricted DB
  access are explicitly out of scope here.

See [`docs/task-lifecycle.md`](../task-lifecycle.md) for the operating model, the
grab/transition API, and the telemetry queries.
