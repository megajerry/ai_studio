"""Pure (DB-free) tests for the EXPERIMENT primitive (ADR-0016).

Covers the comparator/aggregate helpers, the guarded status machine, and the
kill/scale decision rule — all runnable anywhere (no Postgres). The live-DB
end-to-end lifecycle is in ``test_experiment_db.py``.
"""

from __future__ import annotations

import pytest

from runtime.experiment.models import (
    DEFAULT_SCALE_FACTOR,
    Evaluation,
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


# --- comparators / metric helpers -------------------------------------------


def test_metric_meets_all_comparators():
    assert metric_meets(5, 5, ">=") and metric_meets(6, 5, ">=")
    assert not metric_meets(4, 5, ">=")
    assert metric_meets(6, 5, ">") and not metric_meets(5, 5, ">")
    assert metric_meets(4, 5, "<=") and metric_meets(5, 5, "<=")
    assert metric_meets(4, 5, "<") and not metric_meets(5, 5, "<")
    assert metric_meets(5, 5, "==") and not metric_meets(6, 5, "==")


def test_metric_meets_rejects_unknown_comparator():
    with pytest.raises(ValueError):
        metric_meets(1, 1, "≈")


def test_strongly_met_higher_is_better():
    # target 100, factor 1.25 → strong at >= 125.
    assert is_strongly_met(130, 100, ">=", 1.25)
    assert not is_strongly_met(110, 100, ">=", 1.25)  # met but not strong
    assert not is_strongly_met(90, 100, ">=", 1.25)  # missed


def test_strongly_met_lower_is_better():
    # target 100 (a ceiling), factor 1.25 → strong at <= 80.
    assert is_strongly_met(70, 100, "<=", 1.25)
    assert not is_strongly_met(95, 100, "<=", 1.25)  # met but not strong
    assert not is_strongly_met(120, 100, "<=", 1.25)  # missed


def test_strongly_met_equal_is_never_strong():
    assert metric_meets(5, 5, "==")
    assert not is_strongly_met(5, 5, "==", 1.25)


def test_strongly_met_rejects_bad_scale_factor():
    with pytest.raises(ValueError):
        is_strongly_met(1, 1, ">=", 0.5)


def test_metric_spec_validation():
    SuccessMetric(name="x", target=1, comparator=">=").validate_spec()
    with pytest.raises(ValueError):
        SuccessMetric(name="x", target=1, comparator="!!").validate_spec()
    with pytest.raises(ValueError):
        SuccessMetric(name="x", target=1, aggregate="median").validate_spec()


# --- guarded status machine -------------------------------------------------


def test_legal_forward_transitions():
    assert can_transition(ExperimentStatus.PROPOSED, ExperimentStatus.RUNNING)
    assert can_transition(ExperimentStatus.RUNNING, ExperimentStatus.EVALUATED)
    for d in (ExperimentStatus.KEPT, ExperimentStatus.SCALED, ExperimentStatus.KILLED):
        assert can_transition(ExperimentStatus.EVALUATED, d)
    # early kill is allowed (abandon a bet)
    assert can_transition(ExperimentStatus.PROPOSED, ExperimentStatus.KILLED)
    assert can_transition(ExperimentStatus.RUNNING, ExperimentStatus.KILLED)


def test_illegal_transitions_rejected():
    with pytest.raises(IllegalTransition):
        assert_transition(ExperimentStatus.PROPOSED, ExperimentStatus.EVALUATED)  # skips running
    with pytest.raises(IllegalTransition):
        assert_transition(ExperimentStatus.EVALUATED, ExperimentStatus.RUNNING)  # backward
    with pytest.raises(IllegalTransition):
        assert_transition(ExperimentStatus.KEPT, ExperimentStatus.SCALED)  # terminal
    with pytest.raises(IllegalTransition):
        assert_transition(ExperimentStatus.RUNNING, "bogus")  # unknown target


def test_terminal_states():
    for s in (ExperimentStatus.KEPT, ExperimentStatus.SCALED, ExperimentStatus.KILLED):
        assert is_terminal(s)
    for s in (ExperimentStatus.PROPOSED, ExperimentStatus.RUNNING, ExperimentStatus.EVALUATED):
        assert not is_terminal(s)


def test_decision_maps_to_status():
    assert ExperimentDecision.KEPT.to_status() is ExperimentStatus.KEPT
    assert ExperimentDecision.SCALED.to_status() is ExperimentStatus.SCALED
    assert ExperimentDecision.KILLED.to_status() is ExperimentStatus.KILLED


# --- the kill/scale decision rule -------------------------------------------


def _metric(target=100.0, comparator=">=") -> SuccessMetric:
    return SuccessMetric(name="signal", target=target, comparator=comparator)


def _decide(observed, *, target=100.0, comparator=">=", bt=None, bu=None, st=0, su=0.0) -> Evaluation:
    return decide_outcome(
        _metric(target, comparator),
        observed,
        spent_tokens=st,
        spent_usd=su,
        budget_tokens=bt,
        budget_usd=bu,
    )


def test_decide_kept_when_met_within_budget():
    ev = _decide(110, bt=1000, st=500)  # met, not strong, within budget
    assert ev.decision is ExperimentDecision.KEPT and not ev.over_budget


def test_decide_scaled_when_strongly_met():
    ev = _decide(200, bt=1000, st=500)  # 200 >= 100*1.25 → strong
    assert ev.decision is ExperimentDecision.SCALED and not ev.over_budget


def test_decide_killed_when_metric_missed():
    ev = _decide(50, bt=1000, st=500)
    assert ev.decision is ExperimentDecision.KILLED
    assert "missed" in ev.reason


def test_decide_killed_when_over_token_budget_even_if_metric_strong():
    ev = _decide(500, bt=100, st=500)  # strong metric but blew the token budget
    assert ev.decision is ExperimentDecision.KILLED and ev.over_budget
    assert "over budget" in ev.reason


def test_decide_killed_when_over_usd_budget():
    ev = _decide(500, bu=1.0, su=5.0)
    assert ev.decision is ExperimentDecision.KILLED and ev.over_budget


def test_decide_killed_when_no_evidence():
    ev = _decide(None, bt=1000, st=10)
    assert ev.decision is ExperimentDecision.KILLED and "no evidence" in ev.reason


def test_over_budget_helper():
    assert is_over_budget(101, 0.0, 100, None)
    assert is_over_budget(0, 2.0, None, 1.0)
    assert not is_over_budget(50, 0.5, 100, 1.0)
    assert not is_over_budget(50, 0.5, None, None)  # uncapped never over


def test_default_scale_factor_is_sane():
    assert DEFAULT_SCALE_FACTOR >= 1.0
