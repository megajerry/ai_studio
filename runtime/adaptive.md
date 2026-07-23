# `runtime/adaptive` — adaptive orchestration intensity

ADR-0003 says the orchestration cycle is **adaptive, not fixed**:

> more review when a workstream's recent error rate is high, more research in a
> fast-moving domain, throttled by token/time budget.

Before this module the worker already had *lite* triggers — `WORKER_REVIEW=on_risk`
(review only a risky episode) and `WORKER_RETRO=on_fail` (retro only a failed one).
Those scale by **this one episode's** facts. This module generalizes them into a
policy that scales the *base* trigger modes by a workstream's **recent history**
(error rate) and its **budget headroom**, so a workstream that is currently
erroring gets MORE oversight and a clean one that is running out of budget gets
less — all from persisted FACTS, deterministically, and bounded.

It is **off by default** (`ADAPTIVE_INTENSITY` unset / `off`), in which case every
helper returns the caller's base mode unchanged and reads no telemetry — the
worker's existing static behavior is preserved exactly.

## Model

- **Evidence, not vibes.** Every knob is computed from the telemetry the runtime
  already records — never from a model:
  - `recent_error_rate(conn, ws, window)` — the fraction of the last `window`
    **WORK** episodes (`task.finished` events for `work.*` tasks) that went wrong.
    An episode is "errored" if it carried a `verify.failed` / `task.rekicked`
    event, was abandoned (`task.transition → abandoned`), or was the target of a
    `review.flagged` (attributed via its `target_task_id`, since that event is
    emitted on the review task). `0.0` when there is no recent work (no evidence →
    not risky).
  - `recent_activity(conn, ws, hours)` — count of WORK episodes finished in a
    rolling window; the closest FACT the runtime has to ADR-0003's "domain
    velocity" (a fast-moving domain finishes many items per unit time).
- **Budget/time throttle.** `budget_remaining` (as returned by
  `runtime.budget.remaining`) is normalized to a **remaining fraction** in `[0,1]`
  (the tightest configured cap governs; uncapped → `None` → ample). Two thresholds:
  *tight* (`budget_tight`, default `0.15`) and *critical* (`budget_critical`,
  default `0.05`).
- **Deterministic + pure decision core.** `_scale` / `_scale_research` are pure
  functions of `(base, error_rate, budget_fraction, activity, config)`; the DB
  readers only gather the inputs. Same inputs → same mode, always.
- **Bounded.** Every result is one of a small closed set — the same strings the
  worker already understands.

## The escalation rule (`_scale`, shared by review + retro)

In priority order — **the budget throttle beats the error escalation**:

1. budget **critical** → `off` (never pile on extra work when nearly exhausted).
2. error rate **high** (`>= high_error_rate`, default `0.5`) → **escalate** toward
   `always` (review/retro every episode) — but only the **guard** mode
   (`on_risk` / `on_fail`) when the budget is *tight* (still catch risky episodes,
   without reviewing everything).
3. error rate **low** (`<= low_error_rate`, default `0.1`) AND budget **tight** →
   `off` (clean + starved → relax).
4. otherwise → the **base** mode unchanged.

`research_cadence` follows the same shape over `eager | normal | off`: `eager`
when the domain is fast-moving (`activity >= high_activity`) or erroring, throttled
to `normal` when tight and `off` when critical or calm+tight.

## API

| Function | What |
| --- | --- |
| `recent_error_rate(conn, ws, window=None, *, config)` | Fraction of recent WORK episodes that errored, `[0,1]`. |
| `recent_activity(conn, ws, *, hours=None, config)` | WORK episodes finished in the rolling window (velocity proxy). |
| `budget_fraction(budget_remaining) -> float \| None` | Normalize `None` / a float / a `BudgetStatus` to a remaining fraction. |
| `review_mode(conn, ws, base_mode, budget_remaining, *, config) -> "always\|on_risk\|off"` | Effective review trigger. |
| `retro_mode(conn, ws, base_mode, budget_remaining, *, config) -> "always\|on_fail\|off"` | Effective retro trigger. |
| `research_cadence(conn, ws, base_cadence, budget_remaining, *, config) -> "eager\|normal\|off"` | Effective research cadence. |
| `resolve_modes(conn, ws, *, base_review, base_retro, base_research, budget_remaining, config) -> IntensityDecision` | All three at once (one telemetry read) + the facts behind them. The worker's seam. |

When disabled, all of the above return the passed base(s) verbatim without
touching `conn` (proven in tests by passing `conn=None`).

## Configuration (env, with defaults)

| Var | Default | Meaning |
| --- | --- | --- |
| `ADAPTIVE_INTENSITY` | `off` | Master switch (`on`/`1`/`true` → enabled). |
| `ADAPTIVE_ERROR_WINDOW` | `20` | How many recent WORK episodes define "recent". |
| `ADAPTIVE_HIGH_ERROR_RATE` | `0.5` | error_rate ≥ this → escalate. |
| `ADAPTIVE_LOW_ERROR_RATE` | `0.1` | error_rate ≤ this → clean. |
| `ADAPTIVE_BUDGET_TIGHT` | `0.15` | remaining fraction ≤ this → tight (don't escalate to `always`). |
| `ADAPTIVE_BUDGET_CRITICAL` | `0.05` | remaining fraction ≤ this → critical (throttle to `off`). |
| `ADAPTIVE_VELOCITY_WINDOW_HOURS` | `24` | Rolling window for `recent_activity`. |
| `ADAPTIVE_HIGH_ACTIVITY` | `8` | WORK episodes in the window ≥ this → fast-moving. |

## How the worker uses it

`runtime.worker.run_once` resolves the effective modes for each `work.*` episode
via the injectable `resolve_intensity` seam (default `_resolve_intensity_default`):

- It reads the base modes from `WORKER_REVIEW` / `WORKER_RETRO` (today's static
  policy) exactly as before.
- If `ADAPTIVE_INTENSITY` is on, it reads the workstream's `budget.remaining(...)`
  and calls `adaptive.resolve_modes(...)`, then passes the resolved
  `review_mode` / `retro_mode` into `_handle_work` — so a high-error workstream
  gets MORE review/retro and a clean/low-budget one gets less. A change is logged
  with a leak-free rationale (error rate, budget fraction, activity — counts only).
- If off, the base modes pass straight through and **no telemetry or budget is
  read** — behavior-preserving.

The escalation stays **bounded + loop-free**: it only changes *which* of the
existing bounded triggers fire; a review/retro task is still a distinct type that
enqueues nothing, so there is no review-of-a-review / retro-of-a-retro loop.

## Invariants

- **No secrets / PII.** Readers select counts and status strings only; the worker
  logs numbers only (invariants 5 & 6).
- **Caller owns the transaction.** Every reader takes an open `conn` and commits
  only its own read on a non-autocommit connection (like `runtime.tasks` /
  `runtime.budget`).
- **Deterministic + bounded.** Given the same DB state + budget + config, the
  resolved modes are identical and always within the legal set.

## Tests

`runtime/tests/test_adaptive.py` — pure (decision core, budget normalization,
env parsing, bounded outputs, OFF passthrough with `conn=None`) + live-DB
(error-rate/activity from seeded telemetry across every signal type, window
bounding, review/retro escalation on high error rate, relaxation on clean+tight
budget, budget-throttle-beats-escalation) + hermetic worker-wiring (run_once
applies escalated modes; the default OFF resolver preserves static behavior).
