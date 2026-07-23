"""Wilson confidence-interval + Rate tests (pure; no DB, no model).

Pins the KNOWN VALUES the harness relies on so the honest tiny-n signal can't
silently drift: 5/5 → ≈[0.566, 1.0], 7/7 → ≈[0.646, 1.0]. Also proves the
small-sample flag and the None-on-empty convention.
"""

from __future__ import annotations

import math

from evals.stats import INSUFFICIENT_N, Rate, rate, wilson_interval


def test_wilson_known_value_5_of_5():
    lo, hi = wilson_interval(5, 5)
    assert math.isclose(lo, 0.5655, abs_tol=5e-4), lo
    assert math.isclose(hi, 1.0, abs_tol=1e-6), hi


def test_wilson_known_value_7_of_7():
    lo, hi = wilson_interval(7, 7)
    assert math.isclose(lo, 0.6457, abs_tol=5e-4), lo
    assert math.isclose(hi, 1.0, abs_tol=1e-6), hi


def test_wilson_interval_is_bounded_and_ordered():
    lo, hi = wilson_interval(3, 10)
    assert 0.0 <= lo <= hi <= 1.0
    # A 50/100 sample is tighter (smaller width) than a 5/10 sample.
    w_small = wilson_interval(5, 10)
    w_big = wilson_interval(50, 100)
    assert (w_big[1] - w_big[0]) < (w_small[1] - w_small[0])


def test_wilson_zero_n_is_maximally_uninformative():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_clamps_out_of_range_successes():
    # A ratio > 1 (e.g. rekicks/task) must not blow up the interval.
    lo, hi = wilson_interval(15, 10)
    assert 0.0 <= lo <= hi <= 1.0


def test_rate_carries_n_ci_and_insufficient_flag():
    r = rate("precision", 5, 5)
    assert r.value == 1.0 and r.n == 5
    lo, hi = r.ci
    assert math.isclose(lo, 0.5655, abs_tol=5e-4)
    assert r.insufficient is True  # n=5 < 30
    d = r.to_dict()
    assert d["label"] == "precision" and d["n"] == 5
    assert d["ci95"][0] < 1.0 and d["ci95"][1] == 1.0
    assert d["insufficient_sample"] is True


def test_rate_large_n_not_flagged_insufficient():
    r = Rate("verify_pass_rate", 90, 100)
    assert r.n >= INSUFFICIENT_N
    assert r.insufficient is False
    assert r.value == 0.9


def test_rate_zero_denominator_is_none_value():
    r = rate("empty", 0, 0)
    assert r.value is None
    assert r.insufficient is True
    assert r.render().startswith("empty=n/a n=0")


def test_rate_render_shows_ci_and_flag():
    s = rate("recall", 5, 5).render()
    assert "recall=1.0" in s and "n=5" in s
    assert "95%CI=[0.566,1.000]" in s
    assert "INSUFFICIENT(n<30)" in s
