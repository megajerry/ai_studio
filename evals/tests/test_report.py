"""Report assembly/rendering tests (pure; no DB, no model).

Locks in the v2 promise: every reported rate is rendered with n + a Wilson 95% CI,
small samples are flagged INSUFFICIENT, and the telemetry proportions get CIs from
the counts without touching runtime.quality.
"""

from __future__ import annotations

from evals.report import build_report, render_markdown, telemetry_rates


def _verifier_stub():
    return {
        "confusion": {"tp": 5, "fp": 0, "fn": 0, "tn": 2, "support": 7, "positives": 5,
                      "precision": 1.0, "recall": 1.0, "f1": 1.0, "accuracy": 1.0},
        "rates": [
            {"label": "precision", "value": 1.0, "n": 5, "numerator": 5,
             "ci95": [0.5655, 1.0], "insufficient_sample": True},
            {"label": "recall", "value": 1.0, "n": 5, "numerator": 5,
             "ci95": [0.5655, 1.0], "insufficient_sample": True},
        ],
        "cases": [{"name": "c", "check": "marker", "expected_pass": True,
                   "predicted_pass": True, "correct": True}],
        "passed": True,
    }


def _quality_stub():
    return {
        "workstream": None,
        "totals": {"tasks_merged": 90, "tasks_abandoned": 10, "tasks_terminal": 100,
                   "verify_passed": 80, "verify_failed": 20, "rekicks": 5,
                   "model_calls": 100},
        "rates": {"task_success_rate": 0.9, "verify_pass_rate": 0.8,
                  "rekick_rate": 0.05, "error_rate": 0.2727},
        "cost": {"avg_cost_per_completed_task_usd": 0.001,
                 "avg_tokens_per_completed_task": 100},
        "latency": {"avg_latency_per_completed_task_ms": 42},
    }


def test_telemetry_rates_get_cis_from_counts():
    rates = telemetry_rates(_quality_stub())
    by = {r.label: r for r in rates}
    assert set(by) == {"task_success_rate", "verify_pass_rate", "error_rate"}
    # n=100 success rate is NOT flagged insufficient; CI is a real interval.
    sr = by["task_success_rate"]
    assert sr.n == 100 and sr.insufficient is False
    lo, hi = sr.ci
    assert 0.0 < lo < 0.9 < hi < 1.0


def test_build_report_augments_quality_with_ci_and_tags_v2():
    full = build_report(_verifier_stub(), None, _quality_stub(), None)
    assert full["harness"] == "evaluation-harness-v2"
    assert "rates_ci" in full["telemetry_quality_report"]
    assert len(full["telemetry_quality_report"]["rates_ci"]) == 3


def test_markdown_renders_ci_and_insufficient_flag():
    full = build_report(_verifier_stub(), None, _quality_stub(), None)
    md = render_markdown(full)
    assert "Evaluation Harness v2" in md
    assert "95%CI=[0.5655, 1.0]" in md
    assert "INSUFFICIENT (n<30)" in md
    # The honest overclaim correction is present in the rendered report.
    assert "LOGIC/ORACLE test" in md
