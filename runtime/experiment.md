# Experiment primitive (`runtime/experiment/`)

The venture-studio brain's first object (ADR-0016, architecture §11). An
**experiment** is a bounded bet: a hypothesis, a measurable success metric, a
token/$ budget, and an **evidence-based kill/scale verdict**. Generic and
product-agnostic — the machinery here doesn't know what the studio is betting on.

## The object

`Experiment` (`runtime/experiment/models.py`):

| field | meaning |
| --- | --- |
| `workstream` | which workstream owns the bet |
| `hypothesis` | free text — the thing being tested (never emitted in events) |
| `success_metric` | `{name, target, comparator, aggregate}` — what "worked" means |
| `budget_tokens` / `budget_usd` | spend ceilings (null = uncapped) |
| `status` | `proposed → running → evaluated → kept\|scaled\|killed` |
| `decision` | the terminal verdict (`kept`/`scaled`/`killed`) |
| `observed_value`, `spent_tokens`, `spent_usd` | the evidence the verdict was judged on |

`comparator ∈ {>=, >, <=, <, ==}` (allowlisted, never `eval`'d);
`aggregate ∈ {last, first, sum, max, min, mean}` folds an observation series.

## Lifecycle (guarded, mirrors the task state machine)

```
proposed → running → evaluated → (kept | scaled | killed)
   │          │
   └──────────┴──► killed      (abandon a bet before / during a run)
```

Every change goes through `assert_transition`; illegal moves raise
`IllegalTransition`. `kept | scaled | killed` are terminal.

## The kill/scale rule (evidence-based, pure — `decide_outcome`)

Budget is a hard gate, then the metric:

1. **Over budget** (measured spend > ceiling) → **killed** (even if the metric is good).
2. **Metric missed / no evidence** → **killed**.
3. **Metric strongly met** (past the target by `scale_factor`, default 1.25) → **scaled**.
4. **Metric met** → **kept**.

The verdict is computed from telemetry **facts** (spend from `task_cost`, outcomes
from the event log), never a model's self-report.

## API (`runtime/experiment/api.py`)

```python
from runtime.experiment import (
    propose_experiment, start_experiment, record_observation, evaluate_experiment,
    SuccessMetric,
)

exp = propose_experiment(
    conn, workstream="growth", hypothesis="landing B converts better",
    metric=SuccessMetric(name="signup_rate", target=0.10, comparator=">="),
    budget_tokens=200_000, budget_usd=5.0, sink=sink,
)
exp = start_experiment(conn, exp.id, sink=sink, work_items=[
    {"type": "work.run_variant", "payload": {"variant": "B"}, "budget_tokens": 50_000},
])
# ... work runs; it reports evidence as it goes:
record_observation(conn, exp.id, 0.13, sink=sink)
exp = evaluate_experiment(conn, exp.id, sink=sink)   # → kept | scaled | killed
```

- `start_experiment` stamps `experiment_id` into each work item's payload — the
  link `evaluate_experiment` follows to sum spend via `task_cost`.
- A **cost metric** (`cost_usd`, `total_tokens`, `spent_tokens`, …) needs no
  observation: its value is read straight from spend telemetry.
- A **`scaled`** verdict opens a 🛑 approval (`request_approval`, tier `red`, tool
  `experiment.scale`, capability `budget.increase`) for the added budget (ADR-0006)
  and links its id in the `experiment.evaluated` event.

## Events

`experiment.proposed`, `experiment.started`, `experiment.observation`,
`experiment.evaluated` — carrying ids / workstream / metric identity / target /
comparator / budgets / spend / decision only. The `hypothesis` text and all
argument values are **never** emitted (invariants 5 & 6).

## Storage

`experiments` table, migration `0009_experiments.sql` (forward-only, idempotent),
indexed on `(workstream, status)`.

## Tests

- `runtime/tests/test_experiment.py` — pure (comparators, guard, decision rule).
- `runtime/tests/test_experiment_db.py` — live-DB lifecycle: kept/killed/scaled,
  over-budget → killed, scale → 🛑 approval, illegal transitions, no-secret events,
  migration idempotent. Skips cleanly with no reachable DB.
