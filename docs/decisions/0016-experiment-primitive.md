# 0016 — The experiment primitive (venture-studio brain, first object)

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The platform substrate (event log, task queue, policy engine, tools, roles,
approvals, lifecycle state machine, telemetry) is A-grade and verified, but the
**venture-studio value layer had not begun**. Architecture §11 names the moat
explicitly: *how the studio defines experiments, evaluates signal, allocates
resources, and closes the Builder↔Product loop* — "only that top layer is written
from scratch." The studio's whole job is to place many small bets and, on
evidence, **kill the losers and scale the winners**. Nothing in the runtime yet
represented a *bet*. This ADR introduces the first such object: the **experiment**.

## Decision

**A generic `experiment` primitive with an evidence-based kill/scale rule**, built
on top of the existing substrate (it enqueues tagged work via
`runtime.tasks.enqueue_task`, reads spend via `task_cost`, and requests budget via
`runtime.approvals.request_approval` — it edits none of them). It lives in a
self-contained package `runtime/experiment/` and is product-agnostic: the *first
real* experiment needs a product decision, but the machinery does not.

### The object (`runtime/experiment/models.py`)

An `Experiment` = `id, workstream, hypothesis, success_metric {name, target,
comparator, aggregate}, budget_tokens, budget_usd, status, decision,
observed_value, spent_tokens, spent_usd, created_at, started_at, evaluated_at`.
The `success_metric` comparator is a small allowlist (`>= > <= < ==`), never
`eval`'d; `aggregate` (`last/first/sum/max/min/mean`) folds an evidence series.

### Status lifecycle (guarded, mirrors `runtime/task_state.py`)

```
proposed → running → evaluated → (kept | scaled | killed)
   │          │
   └──────────┴──► killed          (abandon a bet before / during a run)
```

`kept | scaled | killed` are terminal and equal the recorded `decision`. A
`TRANSITIONS` map + `assert_transition` make illegal moves raise
`IllegalTransition` — the same discipline as the task machine.

### The kill/scale rule (pure, evidence-based — `decide_outcome`)

Computed from **facts**, not a model claim. Budget is a hard gate, then the metric:

1. **Over budget** (measured spend > declared ceiling) → **`killed`**, even if the
   metric looks good. Spend is a fact.
2. **Metric missed, or no evidence recorded** → **`killed`** (no bet is kept on faith).
3. **Metric strongly met** (met with a margin: `value ≥ target × scale_factor` for
   higher-is-better, `value ≤ target ÷ scale_factor` for lower-is-better; default
   factor 1.25) → **`scaled`**.
4. **Metric met** → **`kept`**.

### The control loop (`runtime/experiment/api.py`)

- `propose_experiment(...)` → a `proposed` row + `experiment.proposed`.
- `start_experiment(...)` → `running`; enqueues work items toward the hypothesis,
  each stamped with `experiment_id` in its payload (the link evaluation follows).
- `record_observation(...)` → a work item reports a measured metric datapoint as an
  `experiment.observation` event (the evidence series).
- `evaluate_experiment(conn, id)` → reads spend (`task_cost` over the tagged tasks)
  and the observed metric (a cost metric like `cost_usd`/`*_tokens` reads straight
  from spend; any other aggregates the observation series), applies `decide_outcome`,
  walks `running → evaluated → <decision>` (guarded), and — on `scaled` — opens a
  🛑 approval (`request_approval`, tier `red`, tool `experiment.scale`,
  capability `budget.increase`) for the added budget (ADR-0006). Emits
  `experiment.evaluated` with the metric + spend + decision.

### Events carry no secret text (invariants 5 & 6)

`experiment.*` events carry ids / workstream / metric identity / target /
comparator / budgets / spend / decision only. The `hypothesis` free-text field and
all argument values are **never** emitted.

## Consequences

- The studio now has a first-class **unit of bet** with a deterministic,
  auditable, replayable kill/scale verdict — the moat's first object.
- The decision is grounded in telemetry the platform already produces (spend from
  `task_cost`, outcomes from the event log), so it cannot be gamed by a model's
  self-report.
- Scaling a winner and over-spending a loser both route through existing controls
  (🛑 approvals; spend facts) rather than new bespoke paths.
- Migration `0009_experiments.sql` is forward-only + idempotent, with a
  `(workstream, status)` index for the primary read path.
- **Out of scope (follow-ups):** a *real* first experiment (needs a product
  decision); real budget *enforcement* that gates mid-run (backlog item 3); wiring
  the evaluate step into an automatic scheduler/PM cadence.

See [`runtime/experiment.md`](../../runtime/experiment.md) for the operating model
and API.
