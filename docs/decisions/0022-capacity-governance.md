# 0022 — Graduated capacity governance (warn → throttle → reserve → hard-stop)

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

The studio is budget-bounded ([ADR-0006](0006-stakeholder-comms.md),
[ADR-0012](0012-telemetry-metrics.md), `docs/cost-model.md` §8): a workstream
runs under a ceiling and *raising* that ceiling is a 🛑 stakeholder decision,
never a silent overspend. `runtime/budget.py` enforces this — but today it is a
**hard binary cap**. `enforce()` lets a call through under `cap_usd`/`cap_tokens`
and, the moment a call would cross the cap, blocks it and raises a 🛑 "raise
budget" approval.

That binary gate has three gaps the stakeholder wants closed. Capacity must be
respected **proactively**, not just hard-capped:

1. **No graduated thresholds.** A workstream at 99% of cap looks identical to one
   at 10% until the wall is hit — there is no early signal to slow down or
   change plan.
2. **No reserve buffer.** When a workstream hits the cap it cannot afford to even
   *react*: the same block that stops normal work also stops the wind-down /
   escalation / hand-off calls needed to fail gracefully. The agent is trapped
   at the wall with no budget to climb down.
3. **No burn-rate projection.** Nothing projects *when* a workstream will exhaust
   its cap, so it cannot be flagged early.

## Decision

Make capacity governance **graduated and deterministic**. Enforcement stays in
the engine (`runtime/budget.py`) — the single source of truth — so no agent can
opt out of it. Four ideas:

### 1. Two-level ceiling (org/key + per-workstream allocation)

A model call is checked against **both** its per-workstream allocation **and** an
org/key-level ceiling. The org ceiling is modeled as an ordinary `budgets` row
under the reserved sentinel workstream **`__org__`** (no new table). Its "spent"
is the SUM across ALL workstreams in the period (`budget.org_spent`), so it is a
true org/key ceiling. The **tighter** of the two wins. A workstream with neither
an allocation row nor an `__org__` row is unconstrained, exactly as before.

### 2. Tiered thresholds → a zone

Each `budgets` row may carry three spent-fraction thresholds in `(0, 1)`, ordered
`warn_frac ≤ throttle_frac ≤ reserve_frac` (suggested defaults **0.70 / 0.85 /
0.90**). From the projected spent-fraction `(spent + estimate) / cap` — taken as
the MAX across the configured resources (the tightest one) — the engine computes
a **zone**:

| zone       | condition                             | behavior |
|------------|---------------------------------------|----------|
| `ok`       | fraction < warn_frac                  | allow; emit `budget.checkpoint` (as today) |
| `warn`     | warn_frac ≤ fraction < throttle_frac  | allow; emit `budget.warn` (non-blocking) |
| `throttle` | throttle_frac ≤ fraction < reserve_frac | allow; emit `budget.throttle` (non-blocking) |
| `reserve`  | reserve_frac ≤ fraction, not over cap | **restricted** — see §3; emit `budget.reserve` |
| `over`     | (spent + estimate) > cap              | block; emit `budget.exceeded` + 🛑 approval |

`over` always wins. `warn`/`throttle` are **non-blocking** telemetry — the call
proceeds — giving a workstream (and, later, a Capacity Steward) an early signal
to compact context, cut scope, or change model tier.

### 3. The reserve buffer — spendable only to react

The band between `reserve_frac × cap` and the hard cap is a **reserve buffer**
held back for reaction. `enforce()` takes a `purpose` argument
(`normal | wind_down | escalation`, default `normal`):

- In the reserve zone a **`normal`** call is **withheld** — it emits
  `budget.reserve` and raises `OverBudget`, **without** a 🛑 approval (this is not
  a ceiling raise). The buffer is preserved.
- A **`wind_down`** or **`escalation`** call is **allowed** through the reserve
  zone, so a workstream can pivot, hand off, summarize, or escalate to the human
  **before** it breaches the hard cap.

This is the "preserve a buffer to react" semantics: the workstream always keeps
enough headroom to fail gracefully instead of face-planting into the wall.

Context compaction ([ADR-0013](0013-context-management.md)) is a canonical
reaction to a `warn`/`throttle`/`reserve` signal: shrinking context is the
cheapest way to lower burn before the buffer is spent.

### 4. Burn-rate projection

`burn_rate()` / `project_exhaustion()` read the same `model.call` source as
`spent()` to compute a recent USD/token burn rate and project time / calls to
exhaustion, so a workstream can be flagged **early** (before it even reaches the
reserve zone). Read-only helpers; leak-free (numbers only).

### Behavior change (intended)

The reserve zone **restricts spend that previously succeeded**: a `normal` call
between `reserve_frac × cap` and the cap that would have gone through under the
old hard cap is now withheld. This is the intended "preserve buffer to react"
semantics, and it is **opt-in** — it only applies to a row that has threshold
fractions configured. **Raising a ceiling remains 🛑** ([ADR-0006](0006-stakeholder-comms.md)):
graduated governance changes *when* and *how* we throttle, never who may raise
the cap.

### Accountability

The PM is accountable for a workstream staying within capacity, exactly as for
the hard cap today. An **optional Capacity Steward** role may later own
watching burn-rate/zone telemetry and driving reactions — but that is a later
track (see Scope); C1 introduces no new role.

## Back-compatibility

Additive and non-breaking:

- Migration `0013_capacity_governance.sql` adds `warn_frac` / `throttle_frac` /
  `reserve_frac` as **nullable columns with NO default**. An existing row (and
  any new row that leaves them NULL) has only `ok` and `over` zones — it behaves
  **exactly as the old hard cap**. Graduated zones exist only once a row's
  fractions are set.
- A workstream with no `budgets` row (and no `__org__` row) is unconstrained, as
  before.
- `enforce()`'s new `purpose` defaults to `normal`; the existing model-call path
  passes nothing and keeps its current behavior until a caller opts in.
- The `over` path is byte-for-byte the old behavior: `budget.exceeded` + a 🛑
  "raise budget" approval + `OverBudget`.

All new events (`budget.warn` / `budget.throttle` / `budget.reserve`) are
**body-free** (invariants 5 & 6): amounts + workstream/period + zone (+ purpose
and reserve headroom on a reserve event) only — never prompts, args, or secrets.

## Scope

- **C1 (this ADR) — the deterministic engine foundation:** the migration, zone
  computation + reserve buffer, burn-rate/projection, the two-level org ceiling,
  and the graduated, `purpose`-aware `enforce()`. Enforcement is deterministic in
  the engine.
- **C2/C3 (later tracks, NOT built here):** the optional Capacity Steward role;
  role-charter self-management prompts that react to zone/burn signals; the
  policy engine tagging each call with its `purpose`/capability so `wind_down` /
  `escalation` intent is derived rather than passed; and capacity telemetry /
  dashboards.

## Consequences

- Workstreams get an early-warning gradient and a guaranteed reaction budget
  instead of a cliff edge.
- Enforcement remains a single deterministic gate no agent can bypass.
- A misconfigured reserve fraction could withhold `normal` work earlier than
  intended; mitigated by the `(0,1)` + ordering CHECK constraints and by the
  fractions being opt-in (NULL = old behavior).

## References

- [ADR-0006 — Stakeholder comms & approval tiers](0006-stakeholder-comms.md)
  (raising a ceiling stays 🛑).
- [ADR-0012 — Cost observability](0012-telemetry-metrics.md) (the `model.call`
  log is the single cost source both `spent()` and burn-rate read).
- [ADR-0013 — Context-window management](0013-context-management.md)
  (context compaction as a budget reaction).
