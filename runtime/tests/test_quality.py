"""Pure unit tests for the CI-aware quality helpers (no DB, never skipped).

``wilson_interval`` / ``_rate_ci`` are pure/deterministic, so they are tested
directly — no Postgres needed. The live outcome-attribution rollup that consumes
them is covered against a real DB in ``test_quality_db.py``.

    pytest runtime/tests/test_quality.py
"""

from __future__ import annotations

import math

import pytest

from runtime.quality import (
    MIN_TRUSTWORTHY_SAMPLE,
    _rate_ci,
    wilson_interval,
)


# --- known-value anchors (from the task spec) -------------------------------


def test_wilson_perfect_small_samples_are_not_certain():
    """A perfect-but-tiny sample must NOT report [1.0, 1.0] — the whole point of
    Wilson over the naive estimate. Anchored to the spec's known values."""
    lo, hi = wilson_interval(5, 5)
    assert (round(lo, 3), round(hi, 3)) == (0.566, 1.0)   # 5/5 → ≈[0.566, 1.0]
    lo, hi = wilson_interval(7, 7)
    assert (round(lo, 3), round(hi, 3)) == (0.646, 1.0)   # 7/7 → ≈[0.646, 1.0]


def test_wilson_zero_sample_is_none():
    assert wilson_interval(0, 0) is None                  # 0/0 → undefined
    assert wilson_interval(5, 0) is None                  # n==0 dominates


def test_wilson_bounds_are_clamped_and_ordered():
    for successes, n in [(0, 3), (1, 4), (3, 5), (10, 10), (1, 100), (50, 100)]:
        lo, hi = wilson_interval(successes, n)
        assert 0.0 <= lo <= hi <= 1.0
        # The point estimate lies inside its own CI.
        p = successes / n
        assert lo - 1e-9 <= p <= hi + 1e-9


def test_wilson_all_failures_lower_bound_is_zero():
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0 and 0.0 < hi < 1.0


def test_wilson_matches_closed_form():
    """Cross-check against an independent longhand computation of the formula."""
    successes, n, z = 12, 40, 1.96
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    expect = (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))
    assert wilson_interval(successes, n) == expect


def test_wilson_rejects_out_of_range_successes():
    with pytest.raises(ValueError):
        wilson_interval(6, 5)
    with pytest.raises(ValueError):
        wilson_interval(-1, 5)


# --- _rate_ci: the honest-rate bundle ---------------------------------------


def test_rate_ci_bundles_rate_n_ci_and_flag():
    r = _rate_ci(5, 5)
    assert r["rate"] == 1.0 and r["successes"] == 5 and r["n"] == 5
    assert (round(r["ci95"][0], 3), r["ci95"][1]) == (0.566, 1.0)
    assert r["insufficient_sample"] is True               # 5 < 30

    r30 = _rate_ci(30, 30)
    assert r30["insufficient_sample"] is False            # exactly at the threshold
    assert r30["n"] == MIN_TRUSTWORTHY_SAMPLE


def test_rate_ci_empty_sample_is_none_safe():
    r = _rate_ci(0, 0)
    assert r["rate"] is None and r["ci95"] is None
    assert r["n"] == 0 and r["successes"] == 0
    assert r["insufficient_sample"] is True               # no sample → untrustworthy
