# 0023 — Graduated, progress-aware recovery ladder (nudge → re-kick → escalate → abandon)

- **Status:** Accepted
- **Date:** 2026-07-24

## Context

The non-agent **supervisor** ([ADR-0004](0004-agent-driven-orchestration.md)) is
the studio's irreducible liveness guarantee: *"no task is ever silently dropped."*
It scans for held tasks whose heartbeat has gone stale (a worker that crashed,
hallucinated, or ran out of budget mid-task) and recovers them. Today it has only
**two moves** (`runtime/supervisor.py`):

1. **Re-kick** — reset `in_progress → up_for_grabs`, clear the claim, bump
   `retries`, re-run from scratch (`task.rekicked`).
2. **Force-abandon** — after `SUPERVISOR_MAX_RETRIES` (5) re-kicks, give up
   (`task.failed_exhausted`).

That binary gate has two gaps:

1. **Re-kick is expensive.** It throws away ALL in-flight progress even for a
   transient stall (a slow tool call, a brief API hiccup). A worker that would
   have recovered in a few seconds instead loses everything and restarts.
2. **A big task can burn every retry making ZERO progress, then just die.** If
   each attempt stalls before it accomplishes anything, the task re-kicks five
   times — five full resets — accomplishing nothing, and is then abandoned. The
   supervisor never notices it is making *no forward progress* and never asks for
   help; it just churns until the counter runs out.

Meanwhile the runtime already records a rich **progress signal** the supervisor
ignores: the reasoning **trajectory** ([ADR-0020](0020-trajectory-observability.md))
and the per-task `model.call` telemetry ([ADR-0012](0012-telemetry-metrics.md)).
A task that is genuinely working produces new trajectory steps and model calls; a
task that is stuck produces none.

## Decision

Replace the binary re-kick/abandon with a graduated, progress-aware **recovery
ladder**, cheapest rung first. Enforcement stays in the **non-LLM** supervisor —
there are still no model calls in this layer, exactly as ADR-0004 requires — and
every rung goes through the single guarded lifecycle
([ADR-0015](0015-task-lifecycle-state-machine.md), `runtime.tasks.transition`) and
emits an event ([invariant 6](../../CLAUDE.md)).

```
stale task
  │
  ├─ Rung 1  NUDGE + GRACE ....... first detection: mark nudged_at, DEFER the
  │                                re-kick for SUPERVISOR_NUDGE_GRACE_S, keep the
  │                                claim. A heartbeat within the grace clears the
  │                                episode → NO reset, progress preserved.
  │
  ├─ Rung 2  RE-KICK ............. still stale after the grace → reset to the grab
  │                                pool, bump retries (the old move). Measure NET
  │                                progress since the last attempt: reset
  │                                no_progress_rekicks on progress, increment on none.
  │
  ├─ Rung 3  ESCALATE-TO-PM ...... no_progress_rekicks >= SUPERVISOR_STUCK_THRESHOLD
  │          (task.stuck)         (default 2, < max_retries): STOP re-kicking, emit
  │                                the task.stuck SIGNAL + supersede the attempt
  │                                (abandoned, reason=stuck_needs_replan) so the PM
  │                                can re-decompose it into smaller subtasks.
  │
  └─ Rung 4  ABANDON ............. progressing but never finishing → at
             (task.failed_exhausted) SUPERVISOR_MAX_RETRIES, force-abandon (unchanged).
```

### Rung 1 — nudge + grace (the cheap rung)

On the FIRST detection of a stall for a task (no `nudged_at` for the current
episode), the supervisor emits `task.nudge`, stamps `nudged_at`, and **defers** the
re-kick for `SUPERVISOR_NUDGE_GRACE_S` (default 45s) **without touching the claim
or heartbeat**. A transient stall — a slow tool, a momentary API timeout — is given
a chance to recover with its **in-flight progress preserved**. If the worker
heartbeats within the grace, `heartbeat()` clears `nudged_at`: the episode is over,
nothing was reset. A heartbeat is deliberately **not** counted as progress (it is
bare liveness — a task can heartbeat while doing nothing), so it never fools the
detector. A **dead** process never heartbeats, so it simply falls through the nudge
to the re-kick rung one grace-window later.

### Rung 2 — re-kick, now progress-aware

Still stale after the grace → the classic re-kick (reset to `up_for_grabs`, clear
the claim, bump `retries`). But each re-kick first measures **net progress since the
last attempt** (`task_made_progress`): does a `model.call` event or a
`trajectory_steps` row exist newer than the task's `last_progress_at` watermark
(baselined at `start_task`, advanced at each re-kick)? Progress resets
`no_progress_rekicks` to 0; no progress increments it. `model.call` events are the
source of `spent_tokens` increments, so counting them subsumes a token-spend check.

### Rung 3 — escalate to the PM EARLY (the key idea)

The progress detector exists to **prevent the endless-reset loop**. Once
`no_progress_rekicks` reaches `SUPERVISOR_STUCK_THRESHOLD` (default **2**, chosen
deliberately **below** `max_retries` = 5), the supervisor **stops re-kicking**: it
records the `stall_reason`, emits the `task.stuck` **signal** (reason code + counts),
and supersedes the attempt via `complete_task(ABANDONED, result={"reason":
"stuck_needs_replan", …})`. This bails out to re-decomposition **early** — after two
fruitless resets, not five — because a task that has made zero progress twice will
almost certainly not succeed on a third identical retry; the right move is to hand
it back to the PM to break into smaller subtasks, not to keep resetting it.

The `task.stuck` event is the ONLY thing R1 produces here. It does **not** enqueue
the PM re-decomposition task — that consumer is **R2** (see Scope).

### Rung 4 — abandon backstop (unchanged)

A task that DOES make progress (so `no_progress_rekicks` stays below the stuck
threshold) but never crosses the finish line still hits the original
`SUPERVISOR_MAX_RETRIES` force-abandon at rung 4. This is the ultimate backstop for
a genuinely-too-big-but-progressing task, and it is byte-for-byte the old behavior.

### Failure-reason capture (attributable API-error deaths)

Separately, the single instrumented model-call site (`runtime/model/call.py`) now
emits a **body-free** `model.call.failed` event when a provider raises anything
other than the handled `ProviderFallback` — carrying the error **class** name +
model/provider/role/task_id, **never** the exception message, prompt, response, or
any secret (it uses `type(exc).__name__`, never `str(exc)`). Today an API-error
death is invisible; this turns it into attributable telemetry. The success path and
budget logic are untouched. This feed is consumed by **R3** (see Scope).

## Configuration

New env knobs (module defaults in parentheses); the DB-outage / reconnect-grace
guards ([ADR-0017](0017-db-resilience-and-remote-access.md)) are unchanged:

- `SUPERVISOR_NUDGE_GRACE_S` (45) — defer the re-kick this long after a nudge. **0
  disables the nudge rung** (re-kick / escalate on first detection, pre-ADR-0023
  timing).
- `SUPERVISOR_STUCK_THRESHOLD` (2) — consecutive no-progress re-kicks before
  escalating to the PM instead of re-kicking. Kept `< SUPERVISOR_MAX_RETRIES` so
  the bail-out is early.

## Back-compatibility

Additive and non-breaking:

- Migration `0014_recovery_ladder.sql` adds `last_progress_at` /
  `no_progress_rekicks` / `stall_reason` / `nudged_at` to `tasks` — all nullable or
  defaulted, so existing rows and behavior are unaffected until the new supervisor
  writes them.
- The lifecycle state machine is **unchanged**: the stuck path reuses the existing
  `→ abandoned` edge, and the nudge/defer rungs change no status at all. No new
  transition edge was needed.
- With `SUPERVISOR_NUDGE_GRACE_S=0` and a high `SUPERVISOR_STUCK_THRESHOLD`, the
  ladder collapses to the exact pre-ADR-0023 re-kick → abandon behavior (this is how
  the existing backstop tests pin the classic rungs in isolation).
- `rekick_task(..., made_progress=None)` (the default) leaves the counters/watermark
  untouched, so any direct caller keeps its old semantics.
- All new events (`task.nudge`, `task.stuck`, `model.call.failed`) are **body-free**
  ([invariants 5 & 6](../../CLAUDE.md)): ids / status / reason CODE / counts /
  grace-seconds / error-class only — never prompts, rationale, or secrets, mirroring
  the `trajectory.*` and `budget.*` discipline.

## Scope

- **R1 (this ADR) — the foundation:** the migration, the graduated ladder in the
  supervisor, the progress detector + `last_progress_at` watermark, the nudge rung,
  the `task.stuck` escalation **signal**, and `model.call.failed` capture. The
  DB-outage resilience + reconnect-grace + scan→write race guards are kept intact.
- **R2 (separate track, NOT built here):** the PM consumer of `task.stuck` — the
  re-decomposition loop that breaks a stuck task into smaller subtasks. R1 only
  emits the signal + supersedes the attempt; it does NOT enqueue the PM task.
- **R3 (separate track, NOT built here):** the failure-pattern → fix → verify loop
  that consumes `model.call.failed` telemetry.

## Consequences

- A transient stall is recovered for the price of one deferred sweep instead of a
  full progress-discarding reset.
- A no-progress task bails to re-decomposition after 2 fruitless resets instead of
  churning through all 5 and dying, so genuinely-too-big work gets help early.
- The supervisor stays a dumb, non-LLM, single-source-of-truth liveness layer; the
  "intelligence" (re-decomposition) is delegated to the PM via an event, not baked
  into the supervisor.
- A mis-tuned `SUPERVISOR_STUCK_THRESHOLD` (too low) could escalate a slow-but-
  progressing task prematurely; mitigated because the detector keys on real work
  signals (a progressing task keeps `no_progress_rekicks` at 0) and the threshold is
  configurable.

## References

- [ADR-0004 — Agent-driven orchestration + the non-agent supervisor](0004-agent-driven-orchestration.md)
  (the liveness layer this extends; still no model calls here).
- [ADR-0015 — Canonical task-lifecycle state machine](0015-task-lifecycle-state-machine.md)
  (every rung goes through the single guarded transition; no new edge added).
- [ADR-0020 — Trajectory observability](0020-trajectory-observability.md)
  (trajectory steps as a progress signal).
- [ADR-0012 — Cost/telemetry](0012-telemetry-metrics.md) (`model.call` as the other
  progress signal + the failure-capture feed).
- [ADR-0013 — Context management](0013-context-management.md) (body-free events;
  concision without dropping verifiable data).
- [ADR-0017 — DB resilience & remote access](0017-db-resilience-and-remote-access.md)
  (reconnect-grace / anti thundering-herd, kept intact).
