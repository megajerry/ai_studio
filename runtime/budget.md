# `runtime/budget` — real per-workstream budget enforcement

The studio is **budget-bounded** (docs/cost-model.md §8): each workstream runs
under a spend ceiling, and *raising* that ceiling is a 🛑 **stakeholder decision**
(ADR-0006), never a silent overspend. Before this module the only budget control
was the router's per-call token *downshift* + `OverBudget` on **dry-run tokens of
one call** (backlog item "Real budget enforcement"). This module adds the missing
half: a durable per-workstream cap read against **real accrued cost**, enforced at
the single instrumented model-call site.

It is the enforcement runtime for the 🛑 "additional budget" class of ADR-0006 —
the Spokesman/WhatsApp surface that renders and resolves those 🛑 approvals is a
separate, existing loop (`runtime/approvals`); here we only decide *when* one is
raised and refuse to spend until it is.

## Model

- **Caps are data.** The `budgets` table (migration `0010_budgets.sql`) holds, per
  `(workstream, period)`, a `cap_usd` and/or `cap_tokens`. Setting/raising a cap is
  a row write (`set_budget`), not a code change. A workstream with **no row is
  unconstrained** — so nothing in the studio changes until a cap is set.
- **Spend is never stored here.** Accrued spend is read **live** from the
  append-only `model.call` events (`spent`), the same cost source as
  `runtime.tasks.task_cost` / `model_rollup` — the event log stays the single
  source of cost truth (ADR-0012). This table holds only ceilings.
- **`period` is a time window.** `daily` (since `date_trunc('day', now())`),
  `monthly` (since `date_trunc('month', now())`), `rolling_30d` (last 30 days),
  or `all_time` (the whole history). A workstream may carry several rows at once
  (e.g. a daily *and* a monthly cap); **every** configured cap is enforced.

## API

| Function | What |
| --- | --- |
| `set_budget(conn, ws, *, period, cap_usd, cap_tokens)` | Idempotent upsert of a cap. |
| `get_budget` / `list_budgets` | Read the configured cap(s). |
| `spent(conn, ws, *, period) -> Spend` | Real accrued `cost_usd` / `tokens` / `calls` in the window. |
| `remaining(conn, ws, *, period) -> BudgetStatus \| None` | Cap − spend (`None` if uncapped). |
| `budget_context(conn, ws, ...) -> BudgetContext \| None` | REAL accrued spend projected onto the policy engine's context. |
| `enforce(conn, ws, *, est_usd, est_tokens, role, task_id, sink)` | The gate — see below. |

## The gate (in the model-call path)

`runtime.model.call.call_model` calls `enforce` **after routing but before the
provider runs**, whenever it is given a `conn`:

```
route → [budget.enforce] → select provider → complete → cost → model.call event → accrue
```

For **every** cap on the workstream, `enforce` computes
`status = cap vs. real accrued spend + this call's estimate` and asks the SAME
predicate the policy engine uses — `BudgetContext.would_exceed`:

- **would exceed** → emit `budget.exceeded`, raise a 🛑 `request_approval`
  (`"raise budget for <ws> (<period>)"`, idempotent per workstream+period so
  repeated blocked calls don't pile up), and raise **`OverBudget`**. The call
  **never runs** — no `model.call`, no spend.
- **under cap** → emit a `budget.checkpoint` (spend/remaining snapshot) and return.

The pending call's cost is estimated from the message length using the same
chars→tokens basis **and output ratio** as the dry-run provider
(`estimate_call_io_tokens`): input **and** output tokens are priced separately
via the routed spec, so the estimate accounts for output (which usually bills at
a higher rate) rather than pricing the whole token sum at the input rate — the
latter was systematically low and could let a call slip just past a cap. Note
**dry-run calls accrue cost too** (their `model.call` cost is computed
identically), so a keyless studio still enforces real budgets.

**Provider fallback is gated too.** If the routed provider raises
`ProviderFallback` (e.g. the flat-rate coding harness times out), `call_model`
reassigns to the next model in the tier chain — usually a pricier metered model —
and **re-runs `enforce` against that fallback spec before retrying**. So a
fallback that would breach the cap is blocked (`budget.exceeded` + 🛑 approval +
`OverBudget`), not silently run and only accounted after the fact. An in-budget
fallback proceeds unchanged.

`OverBudget` here is distinct from `runtime.model.router.OverBudget`: the router's
is about a *routing tier* with no cheaper fallback; this is the hard
**per-workstream ceiling**.

## Consistency with the policy engine

`enforce` and `runtime.policy.decide` share one decision predicate. Policy's
`BudgetContext` now carries **both** a token cap and a USD cap (either may be set);
`budget_context(...)` builds one from the real `model.call` log, so passing it as
`PolicyRequest.budget` makes `decide` escalate an over-cap action to
`NEEDS_APPROVAL` (🛑) using *actual* per-workstream spend — not just one task's
dry-run tokens (ADR-0006/0012, M2-consistent).

## Events (leak-free — invariants 5 & 6)

`budget.exceeded` / `budget.checkpoint` carry only numbers + the workstream/period:
`cap_usd`, `cap_tokens`, `spent_usd`, `spent_tokens`, `est_usd`, `est_tokens`,
`remaining_usd`, `remaining_tokens` (+ a numeric `reason` on exceed). Never a
prompt, argument, or secret.
