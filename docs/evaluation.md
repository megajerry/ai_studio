# Evaluation — the empirical quality framework (harness v2)

_Answers the stakeholder question: "do we have an empirical way to understand
component quality?" — with numbers you can re-run, and an honest line between what
is measurable **now** (dry-run) vs what needs **real models at go-live**._

## What changed in v2 (and the overclaim it corrects)

Harness v1 reported `precision = recall = 1.0` on 7 hand-seeded Verifier cases. The
stakeholder's critique was correct and is worth stating plainly:

> **A `1.0` on `n=7` hand-seeded cases is NOT a trustworthy statistical quality
> metric — it is a LOGIC / ORACLE test with a huge confidence interval.**

Concretely, the Wilson 95% confidence interval for a perfect 5-of-5 defect-recall
score is **≈ [0.566, 1.0]**, and for 7-of-7 accuracy it is **≈ [0.646, 1.0]**. The
point estimate is `1.0`, but the *true* rate could plausibly be anywhere down to
~0.57. That is not a quality estimate; it is a statement that "the gate got the 7
cases we wrote right." v1 presented the bare `1.0` and so **overclaimed**. v2 fixes
this on both fronts the stakeholder named:

1. **Every rate is now reported WITH its sample size `n` and a Wilson 95% CI**, and
   any small sample (`n < 30`) is flagged `INSUFFICIENT` — so the tiny-`n` weakness
   is *visible in the numbers*, never hidden behind a `1.0`.
   (`evals/stats.py`; `wilson_interval` is defined locally and independent of
   `runtime.quality`.)
2. **The real-outcome eval mechanism is in place NOW, keyless** — a swappable
   LLM-as-judge running against the deterministic dryrun provider today and swapping
   to a real model at go-live with **zero code change** (provider selection only),
   plus a record/replay scaffold so real-model runs are reproducible in CI.

The seeded Verifier P/R eval is **kept** (it is a valuable *logic/oracle* regression
test — it proves the gate's wiring catches the defects we can enumerate). It is just
no longer *mislabeled* as a statistical quality metric.

## The honest constraint

Everything is currently **dry-run / keyless** (no provider keys — see
[`state/backlog.md`](../state/backlog.md) boundary items). That means we can measure,
empirically and today:

- **mechanism correctness** — does the evidence gate catch the defects we seed? does
  the PM emit a well-formed decomposition? does the judge/telemetry plumbing run?
- **structural quality** — plan shape, DAG validity, per-item criteria.
- **ops health** — success / verify / re-kick / error rates and cost/latency per
  task, from the live event log.

It does **not** yet measure real **outcome quality** (is the produced work actually
good?), because a dryrun model returns deterministic stubs, not real artifacts and
not real judgments. The *mechanism* for outcome quality is fully wired now (see
[Swappable judge](#3-swappable-llm-as-judge-the-real-outcome-mechanism)); only the
*judging model* is a stand-in until keys land. We say so plainly rather than
reporting a fake quality number.

## What is measured now

### 1. Corpus-as-data

The seeded cases used to be hardcoded in Python. They now live in versioned data
files under [`evals/corpus/`](../evals/corpus/) so the corpus **grows by editing
data, not code** (designed to scale to hundreds of cases):

- `verifier_cases.yaml` — the labeled `(artifact, criterion, expected pass/fail)`
  Verifier corpus (with `{good_marker}`/`{bad_marker}` templated to per-run unique
  markers so cases never collide).
- `pm_goals.yaml` — the labeled PM decomposition goals.
- `rubrics.yaml` — the judge rubrics.

`evals/corpus.py` is the single loader (`load_verifier_cases`, `load_pm_goals`,
`load_rubric`). Every v1 case is preserved (2 GOOD + 5 planted defects = 7).

### 2. Seeded-defect Verifier precision/recall — now with confidence intervals

A labeled corpus of GOOD work and **deliberately-planted** BAD work is run through
the REAL [`runtime.roles.verifier.verify`](../runtime/roles/verifier.py) gate, then
scored as a binary defect classifier (**positive class = defective**):

- horizontal `marker` checker — including a **hallucinated-success** defect (the
  Executor asserts `ok=True` but the artifact lacks the marker → the gate must FAIL
  it on evidence, per [ADR-0014](decisions/0014-validation-rigor.md));
- a reference `video_audit` domain checker (registered on the pluggable
  [checker seam](../runtime/roles/checkers.py) — no Verifier change) with
  wrong-duration and missing-captions defects.

`recall` = of the truly-defective artifacts, how many the gate CAUGHT (a miss is a
defect that slipped through — the dangerous error). `precision` = of the artifacts it
flagged, how many were truly defective (a false alarm on good work). **Each rate is
reported with `n` + a Wilson 95% CI + the `INSUFFICIENT` flag.**

**Sample output (dry-run, actual):**

```
=== Verifier seeded-defect eval (dry-run) ===
  cases=7 defective=5 f1=1.0 accuracy=1.0
  confusion tp=5 fp=0 fn=0 tn=2 (positive=defective)  passed=True
  rates (each with n + Wilson 95% CI):
    precision=1.0 n=5 95%CI=[0.566,1.000] INSUFFICIENT(n<30)
    recall=1.0 n=5 95%CI=[0.566,1.000] INSUFFICIENT(n<30)
    accuracy=1.0 n=7 95%CI=[0.646,1.000] INSUFFICIENT(n<30)
  NOTE: tiny hand-seeded corpus -> a LOGIC/ORACLE test with WIDE CIs (n<30), NOT a
        statistical quality estimate.
```

The metric arithmetic is unit-tested so a real regression (a passed defect) shows up
as `recall < 1.0` rather than hiding, and the CI known-values (5/5 → ≈[0.566, 1.0];
7/7 → ≈[0.646, 1.0]) are pinned in `evals/tests/test_stats.py`.

### 3. Swappable LLM-as-judge (the real-outcome mechanism)

`evals/judge.py` is the mechanism the stakeholder asked to have in place NOW. A
`Judge` scores an item against a rubric and returns a verdict/score, going through
the ONE instrumented call site
([`runtime.model.call.call_model`](../runtime/model/call.py), ADR-0012):

- **Today** it runs against the **dryrun provider** — deterministic, keyless. Because
  a dryrun model cannot really judge, its verdict is a **deterministic stub** derived
  from the model call, flagged `dry_run=True` and treated as a **mechanism** signal,
  never a quality estimate.
- **At go-live**, unsetting `MODELS_DRY_RUN` (or a provider key becoming present)
  makes `call_model` route to the real adapter. The judge sends the SAME rubric
  prompt and parses the model's JSON verdict `{"verdict","score","rationale"}`. This
  is **zero code change — provider selection only**; proven in
  `evals/tests/test_judge.py`, where a recorded real-model JSON verdict is parsed by
  the identical judge code path (`dry_run=False`).

### 4. Trajectory-level decision-quality eval

`evals/trajectory_eval.py` reads a **persisted** PM reasoning trajectory from the
`trajectories` / `trajectory_steps` tables (ADR-0020) and scores its *decision
quality* via the swappable judge against the `pm_decision_quality` rubric. This is a
real-outcome eval running on **real persisted state**; the only stand-in is the
judging model. `seed_demo_trajectory` writes one well-formed PM episode so the eval
always has real state to score.

**Sample output (dry-run, actual):**

```
=== PM trajectory decision-quality eval (swappable judge, dry-run) ===
  trajectory=<uuid> rubric=pm_decision_quality
  verdict passed=True score=0.8561 provider=dryrun dry_run=True harness_passed=True
  NOTE: dryrun judge = MECHANISM signal only; a real model judges the same
        trajectory at go-live with no code change.
```

On a dryrun judge, `harness_passed` means the MECHANISM ran (a deterministic verdict
was produced); on a real judge it is the model's actual pass/fail.

### 5. Record/replay scaffold (reproducible real-model runs)

`evals/replay.py` is a VCR-style cassette (`OFF` / `REPLAY` / `RECORD`) so real-model
judge I/O can be **recorded once and replayed deterministically in CI** — a REPLAY
miss raises loudly rather than silently escalating to a live/paid call. The dryrun
judge needs no recording, but the seam exists and is round-trip tested
(`evals/tests/test_replay.py`) so real-model runs are reproducible the day keys land.

### 6. PM decomposition structural eval

Runs the (dry-run) [PM](../runtime/roles/pm.py) on the labeled goals and scores the
plan: **≥1 work item**, **every item has a checkable criterion**, **acyclic DAG**,
and **dependencies reference real siblings**. The pass rate is reported with n + CI.

### 7. Telemetry quality rollup

`quality_report(conn, workstream=None)` rolls the append-only event log +
`task_transitions` up to a workstream quality report. In v2 the report layer
(`evals/report.py`) augments its binomial proportions (`task_success_rate`,
`verify_pass_rate`, `error_rate`) with Wilson CIs computed from the counts the rollup
already returns — **without touching `runtime.quality`** (the eval and telemetry
tracks stay independent). `rekick_rate` is a per-task *ratio* (can exceed 1), not a
proportion, so it is reported as a bare ratio (no CI).

| metric | formula |
| --- | --- |
| `task_success_rate` | merged / (merged + abandoned) |
| `verify_pass_rate` | verify.passed / (verify.passed + verify.failed) |
| `rekick_rate` | task.rekicked / (merged + abandoned) — *ratio, no CI* |
| `error_rate` | (abandoned + verify.failed) / (terminal + verify.passed + verify.failed) |
| `avg_cost_per_completed_task_usd` | model spend on merged tasks / merged count |
| `avg_tokens_per_completed_task` | tokens on merged tasks / merged count |
| `avg_latency_per_completed_task_ms` | lifecycle latency of merged tasks / merged count |

Cost/tokens are dry-run estimates now and become real spend at go-live with no code
change.

### 8. Coverage (structural test-quality)

Test coverage of `runtime/`, `evals/`, and `spokesman/`, wired via `pytest-cov`
(config in [`.coveragerc`](../.coveragerc)).

```bash
pip install -r runtime/requirements-dev.txt      # pytest-cov + coverage[toml]
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
make coverage          # runs the suite under coverage; term + htmlcov/ report
```

> Run `make coverage` on a networked/host environment (the off-host sandbox may lack
> PyPI access to install `pytest-cov`). The command, config, and dev-requirements are
> all in place.

## Running the harness

```bash
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
python -m evals                                    # print all metrics (n + CI on each)
python -m evals --json state/eval-report.json --markdown state/eval-report.md
make evals                                         # same, writes reports to state/
```

The Verifier eval + metric arithmetic need **no database** (they always run, with n +
CI); the PM structural eval, the trajectory eval, and the telemetry rollup need a
reachable Postgres and are skipped cleanly otherwise (the harness never hangs). Exit
code is `0` when every eval that ran passed.

Tests: `python -m pytest evals/ -q` (pure + live-DB, keyless).

## Deferred to go-live

These need real provider keys / real integrations and are intentionally **not** faked
now. Note the boundary has *narrowed* since v1: the judge, rubrics, corpus, CIs, and
record/replay are all done — the remaining gap is the real *model*, not the
*mechanism*.

- **Real-model judging** — the swappable judge (`evals/judge.py`) runs a real model.
  No code change; unset `MODELS_DRY_RUN` / provide a key, optionally record a cassette
  for reproducible CI. The dryrun verdict flips from a mechanism stub to a real
  outcome score.
- **Real-model golden-set evals** — a curated set of (input → expected-quality
  output) per role, scored by the same judge; plugs into the same report shape.
- **Real-integration smoke** — end-to-end against Docker / Qdrant / live providers /
  WhatsApp (all dry-run/stubbed today).
- **Real spend/latency numbers** — `runtime.quality` already computes them; they
  become *real* once model calls hit real providers.
