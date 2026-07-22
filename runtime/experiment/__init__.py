"""The EXPERIMENT primitive — the venture-studio brain's first object (ADR-0016).

Architecture §11's moat is *how the studio defines an experiment, evaluates the
signal, and decides to kill or scale.* This package is that top layer, built from
scratch on top of the existing substrate (event log, task queue, approvals):

- :mod:`runtime.experiment.models` — the typed :class:`Experiment` + success
  metric, the guarded status machine (``proposed → running → evaluated →
  kept|scaled|killed``), and the pure, evidence-based kill/scale rule.
- :mod:`runtime.experiment.api` — persist + lifecycle: ``propose_experiment`` →
  ``start_experiment`` (enqueues tagged work) → ``record_observation`` →
  ``evaluate_experiment`` (reads telemetry, decides, 🛑 on scale).

Kept as a self-contained package (not re-exported from :mod:`runtime`) so it is a
disjoint, independently reviewable addition to the runtime.
"""

from .api import (
    EVENT_EVALUATED,
    EVENT_OBSERVED,
    EVENT_PROPOSED,
    EVENT_STARTED,
    evaluate_experiment,
    get_experiment,
    list_experiments,
    propose_experiment,
    record_observation,
    start_experiment,
)
from .models import (
    DEFAULT_SCALE_FACTOR,
    Evaluation,
    Experiment,
    ExperimentDecision,
    ExperimentStatus,
    IllegalTransition,
    SuccessMetric,
    assert_transition,
    can_transition,
    decide_outcome,
    is_over_budget,
    is_strongly_met,
    is_terminal,
    metric_meets,
)

__all__ = [
    # models + pure logic
    "Experiment",
    "SuccessMetric",
    "Evaluation",
    "ExperimentStatus",
    "ExperimentDecision",
    "IllegalTransition",
    "DEFAULT_SCALE_FACTOR",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "decide_outcome",
    "is_over_budget",
    "is_strongly_met",
    "metric_meets",
    # api
    "propose_experiment",
    "start_experiment",
    "record_observation",
    "evaluate_experiment",
    "get_experiment",
    "list_experiments",
    "EVENT_PROPOSED",
    "EVENT_STARTED",
    "EVENT_OBSERVED",
    "EVENT_EVALUATED",
]
