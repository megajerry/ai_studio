# Evaluation — the empirical quality framework (harness v1)

_Answers the stakeholder question: "do we have an empirical way to understand
component quality?" — with numbers you can re-run, and an honest line between what
is measurable **now** (dry-run) vs what needs **real models at go-live**._

## The honest constraint

Everything is currently **dry-run / keyless** (no provider keys — see
[`state/backlog.md`](../state/backlog.md) boundary items). That means we can
measure, empirically and today:

- **mechanism correctness** — does the evidence gate actually catch defects? does
  the PM emit a well-formed decomposition? does the telemetry add up?
- **structural quality** — plan shape, DAG validity, per-item criteria.
- **ops health** — success/verify/re-kick/error rates and cost/latency per task,
  from the live event log.

It does **not** yet measure real **outcome quality** (is the produced work
actually good?), because a dry-run model produces deterministic stubs, not real
artifacts. Those evals are designed-for and slot in at go-live (see
[Deferred to go-live](#deferred-to-go-live)). We say so plainly rather than
reporting a fake quality number.

## What is measured now

### 1. Coverage (structural test-quality)

Test coverage of `runtime/`, `evals/`, and `spokesman/`, wired via `pytest-cov`
(config in [`.coveragerc`](../.coveragerc)).

```bash
pip install -r runtime/requirements-dev.txt      # pytest-cov + coverage[toml]
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
make coverage          # runs the suite under coverage; term + htmlcov/ report
```

> **Status in the build sandbox:** coverage was **wired but not measured here** —
> the off-host build environment has no PyPI access to install `pytest-cov`
> (`pip install` fails on SSL/network). Run `make coverage` on the host (which has
> the deps) to get the real percentage. The command, config, and dev-requirements
> are all in place; only the number awaits a networked/host run.

### 2. Seeded-defect Verifier precision/recall (`evals/verifier_eval.py`)

A **labeled** corpus of `(artifact, criterion, expected pass/fail)` cases — known
GOOD work and **deliberately-planted** BAD work — is run through the REAL
[`runtime.roles.verifier.verify`](../runtime/roles/verifier.py) gate, then scored
as a binary defect classifier (**positive class = defective**):

- horizontal `marker` checker — including a **hallucinated-success** defect (the
  Executor asserts `ok=True` but the artifact lacks the marker → the gate must
  FAIL it on evidence, per [ADR-0014](decisions/0014-validation-rigor.md));
- a reference `video_audit` domain checker (defined in the eval, registered on the
  pluggable [checker seam](../runtime/roles/checkers.py) — no Verifier change) with
  wrong-duration and missing-captions defects.

`recall` = of the truly-defective artifacts, how many the gate CAUGHT (a miss is a
defect that slipped through — the dangerous error). `precision` = of the artifacts
it flagged, how many were truly defective (a false alarm on good work). The eval
proves both are `1.0` on the seeded corpus; the metric arithmetic is unit-tested
so a real regression (a passed defect) shows up as `recall < 1.0` rather than
hiding.

**Sample output (dry-run, actual):**

```
=== Verifier seeded-defect eval (dry-run) ===
  cases=7 defective=5 precision=1.0 recall=1.0 f1=1.0 accuracy=1.0
  confusion tp=5 fp=0 fn=0 tn=2 (positive=defective)  passed=True
    ok marker_good                                  expect_pass=True  got=True
    ok marker_missing_hallucinated_success          expect_pass=False got=False
    ok video_duration_too_short                     expect_pass=False got=False
    ok video_missing_captions_hallucinated_success  expect_pass=False got=False
    ...
```

### 3. PM decomposition structural eval (`evals/pm_eval.py`)

Runs the (dry-run) [PM](../runtime/roles/pm.py) on labeled goals and scores the
plan it produces: **≥1 work item**, **every item has a checkable criterion**, the
**dependency DAG is acyclic**, and **dependencies reference real siblings**. The
scorer is pure (`score_decomposition`) so a deliberately BAD plan (empty, missing
criteria, cyclic, dangling deps) is unit-tested to prove the eval FLAGS it.

**Sample output (dry-run, actual):**

```
=== PM structural decomposition eval (dry-run) ===
  goals=3 passed=True
    ok Prove the studio operates end-to-end in dry-run.  decision=planned items=2 criteria=True acyclic=True deps_sane=True
    ...
```

### 4. Telemetry quality rollup (`runtime/quality.py`)

`quality_report(conn, workstream=None)` rolls the append-only event log +
`task_transitions` (ADR-0012 / ADR-0015) up to a **workstream quality report** —
reusing the same telemetry the per-task rollups (`task_lifecycle`, `task_cost`,
`model_rollup`) read. Metrics (each rate is `None` when its denominator is 0):

| metric | formula |
| --- | --- |
| `task_success_rate` | merged / (merged + abandoned) |
| `verify_pass_rate` | verify.passed / (verify.passed + verify.failed) |
| `rekick_rate` | task.rekicked / (merged + abandoned) |
| `error_rate` | (abandoned + verify.failed) / (terminal + verify.passed + verify.failed) |
| `avg_cost_per_completed_task_usd` | model spend on merged tasks / merged count |
| `avg_tokens_per_completed_task` | tokens on merged tasks / merged count |
| `avg_latency_per_completed_task_ms` | lifecycle latency of merged tasks / merged count |

This is what the **Retro** and **Reviewer** read to judge how a workstream is
operating, and the shape a future **Grafana** panel renders. Cost/tokens are
dry-run estimates now and become real spend at go-live with no code change.

## Running the harness

```bash
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
python -m evals                                    # print all metrics
python -m evals --json state/eval-report.json --markdown state/eval-report.md
make evals                                         # same, writes reports to state/
```

The Verifier eval + metric arithmetic need **no database** (they always run); the
PM structural eval + telemetry rollup need a reachable Postgres and are skipped
cleanly otherwise (the harness never hangs). Exit code is `0` when every eval that
ran passed.

Tests: `python -m pytest evals/ -q` (pure + live-DB, keyless).

## Deferred to go-live

These need real provider keys / real integrations and are intentionally **not**
faked now:

- **Real-model golden-set evals** — a curated set of (input → expected-quality
  output) per role, scored against a real model's output.
- **LLM-as-judge OUTCOME evals** — a judge model rating real artifacts for quality
  (the seams — structured criteria, the checker registry — are already in place;
  only the real model is missing).
- **Real-integration smoke** — end-to-end against Docker / Qdrant / live model
  providers / WhatsApp (all dry-run/stubbed today).
- **Real spend/latency numbers** — `runtime.quality` already computes them; they
  become *real* once model calls hit real providers.

When the keys land, the golden-set + judge evals plug into the same harness (a new
`evals/*_eval.py` producing the same report shape) and `quality_report` starts
reporting real cost/quality — no change to the framework.
