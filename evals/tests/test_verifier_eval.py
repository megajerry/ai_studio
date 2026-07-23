"""Seeded-defect Verifier eval tests — keyless, no DB required.

Proves the REAL Verifier evidence gate (run over the labeled corpus) catches every
planted defect (recall 1.0) without false-alarming on good work (precision 1.0),
and specifically that a *hallucinated-success* artifact (claims ok, marker absent)
is FAILED on evidence — the defect the gate must catch.
"""

from __future__ import annotations

from evals.verifier_eval import default_cases, run_verifier_eval


def test_seeded_defect_corpus_has_known_good_and_bad():
    cases = default_cases()
    goods = [c for c in cases if c.expected_pass]
    bads = [c for c in cases if not c.expected_pass]
    assert len(goods) >= 2 and len(bads) >= 3
    # at least one deliberately-planted hallucinated-success defect (claims ok).
    assert any((not c.expected_pass) and c.claimed_ok for c in bads)


def test_verifier_gate_catches_all_planted_defects(tmp_path):
    result = run_verifier_eval(conn=None, scratch=str(tmp_path))
    cm = result.confusion
    # Every defect caught, no good work flagged.
    assert cm.recall == 1.0, f"a defect slipped through: {cm.to_dict()}"
    assert cm.precision == 1.0, f"good work was false-flagged: {cm.to_dict()}"
    assert cm.fn == 0 and cm.fp == 0
    assert cm.support == len(result.cases)
    assert all(c["correct"] for c in result.cases)


def test_hallucinated_success_is_failed_on_evidence(tmp_path):
    result = run_verifier_eval(conn=None, scratch=str(tmp_path))
    by_name = {c["name"]: c for c in result.cases}
    # The Executor CLAIMED ok=True, but the marker is absent -> gate must FAIL it.
    planted = by_name["marker_missing_hallucinated_success"]
    assert planted["claimed_ok"] is True
    assert planted["predicted_pass"] is False
    assert planted["expected_pass"] is False and planted["correct"]
    # And the video defect that also claimed success (missing captions).
    vid = by_name["video_missing_captions_hallucinated_success"]
    assert vid["claimed_ok"] is True and vid["predicted_pass"] is False


def test_a_missed_defect_would_drop_recall():
    """Guard the metric wiring: if the gate had PASSED a defect, recall drops below
    1.0 (so a real regression in the gate cannot hide behind the arithmetic)."""
    from evals.metrics import confusion_from_labels
    # Same shape as the corpus but with one defect wrongly passed.
    rows = [(True, True), (True, True), (False, False), (False, False),
            (False, True)]  # <- the injected miss
    cm = confusion_from_labels(rows)
    assert cm.recall < 1.0 and cm.fn == 1
