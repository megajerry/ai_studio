"""AI Studio — Evaluation Harness v2 (the empirical quality framework).

The stakeholder asked: *"do we have an empirical way to understand component
quality?"* — then sharpened it: *"precision/recall = 1.0 on n=7 hand-seeded cases is
NOT a trustworthy statistical quality metric — it's a logic/oracle test with a huge
confidence interval; and the mechanism for real-outcome evals must be in place NOW
even though real models aren't running."* Harness v2 answers both.

What is measured NOW (dry-run, keyless, no secrets):

- **Seeded-defect Verifier precision/recall** (:mod:`evals.verifier_eval`) — a
  labeled corpus (now **corpus-as-data** in ``evals/corpus/*.yaml``, so it grows by
  editing data) run through the REAL Verifier evidence gate and scored as a defect
  classifier. **Reported WITH n + a Wilson 95% CI** (:mod:`evals.stats`) so the
  tiny-``n`` weakness is visible: on the seeded set this is a LOGIC/ORACLE test with
  a wide CI (≈[0.57, 1.0] at 5/5), NOT a statistical quality estimate.
- **PM decomposition structural eval** (:mod:`evals.pm_eval`) — scores plan shape
  (>=1 item, per-item criteria, acyclic DAG, sane deps); pass rate carries n + CI.
- **PM trajectory decision-quality eval** (:mod:`evals.trajectory_eval`) — reads a
  PERSISTED PM trajectory (``trajectories``/``trajectory_steps``) and scores its
  decision quality via the **swappable LLM-as-judge** (:mod:`evals.judge`) against a
  rubric. Runs on the dryrun provider today (deterministic MECHANISM signal); a real
  model judges the same trajectory at go-live with ZERO code change.
- **Telemetry quality rollup** (:mod:`runtime.quality`) — per-workstream ops/quality
  metrics; proportions rendered with n + Wilson CI in :mod:`evals.report`.

Real-outcome mechanism, in place now (keyless): the **swappable judge**
(:mod:`evals.judge`) goes through the single instrumented call site
(:func:`runtime.model.call.call_model`), and a **record/replay** scaffold
(:mod:`evals.replay`) lets real-model judge I/O be recorded once and replayed
deterministically in CI. The ONLY thing deferred to go-live is the *judging model*
itself; every seam around it exists and is exercised today.

Entrypoint::

    python -m evals            # run every eval, print the metrics (n + CI on each)
    python -m evals --json out.json --markdown out.md   # also write a report
"""

from __future__ import annotations

from .metrics import Confusion, confusion_from_labels
from .stats import INSUFFICIENT_N, Rate, rate, wilson_interval

__all__ = [
    "Confusion",
    "confusion_from_labels",
    "wilson_interval",
    "Rate",
    "rate",
    "INSUFFICIENT_N",
]
