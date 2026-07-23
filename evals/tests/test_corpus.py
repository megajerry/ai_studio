"""Corpus-as-data loader tests (pure; no DB, no model).

Proves the seeded cases load from the versioned data files (not hardcoded Python),
that the count/coverage is preserved from v1, and that unique markers are templated
in so cases never collide.
"""

from __future__ import annotations

from evals.corpus import (
    Rubric,
    load_pm_goals,
    load_rubric,
    load_rubrics,
    load_verifier_cases,
)


def test_verifier_corpus_loads_from_data_with_full_coverage():
    cases = load_verifier_cases()
    # v1 coverage preserved: 2 GOOD + 5 planted defects = 7.
    assert len(cases) == 7
    goods = [c for c in cases if c.expected_pass]
    bads = [c for c in cases if not c.expected_pass]
    assert len(goods) == 2 and len(bads) == 5
    # Both checker families present.
    assert {c.check for c in cases} == {"marker", "video_audit"}
    # At least one hallucinated-success defect (claims ok on defective work).
    assert any((not c.expected_pass) and c.claimed_ok for c in bads)


def test_markers_are_templated_unique_and_present_in_good_content():
    cases = {c.name: c for c in load_verifier_cases()}
    good = cases["marker_good"]
    # {good_marker} substituted -> a real marker, present in its own content.
    assert good.marker and good.marker.startswith("studio-ok:")
    assert good.marker in good.content
    # good vs bad markers differ (no collision).
    bad = cases["marker_missing_hallucinated_success"]
    assert bad.marker != good.marker
    # the hallucinated-success artifact does NOT contain its marker.
    assert bad.marker not in (bad.content or "")


def test_two_loads_generate_fresh_markers():
    a = {c.name: c for c in load_verifier_cases()}["marker_good"].marker
    b = {c.name: c for c in load_verifier_cases()}["marker_good"].marker
    assert a != b  # per-load unique (mirrors the old uuid4 behavior)


def test_pm_goals_load_from_data():
    goals = load_pm_goals()
    assert len(goals) == 3
    assert all(isinstance(g, str) and g.strip() for g in goals)


def test_rubrics_load_and_lookup():
    rubrics = load_rubrics()
    assert "pm_decision_quality" in rubrics
    r = load_rubric("pm_decision_quality")
    assert isinstance(r, Rubric)
    assert r.id == "pm_decision_quality"
    assert len(r.criteria) >= 3
    assert 0.0 < r.pass_threshold <= 1.0


def test_load_missing_rubric_raises():
    try:
        load_rubric("does_not_exist")
    except KeyError:
        return
    raise AssertionError("expected KeyError for a missing rubric")
