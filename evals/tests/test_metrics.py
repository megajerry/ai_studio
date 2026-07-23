"""Pure tests for the classification metrics (no DB, no model, no filesystem)."""

from __future__ import annotations

import math

from evals.metrics import Confusion, confusion_from_labels


def test_confusion_from_labels_maps_pass_fail_to_defect_matrix():
    # (expected_pass, predicted_pass)
    rows = [
        (True, True),    # good passed            -> tn
        (True, False),   # good wrongly flagged   -> fp
        (False, False),  # defect caught          -> tp
        (False, True),   # defect MISSED          -> fn
        (False, False),  # defect caught          -> tp
    ]
    cm = confusion_from_labels(rows)
    assert (cm.tp, cm.fp, cm.fn, cm.tn) == (2, 1, 1, 1)
    assert cm.support == 5
    assert cm.positives == 3  # three truly-defective cases


def test_precision_recall_f1_arithmetic_including_a_missed_defect():
    # 2 caught defects, 1 missed defect (fn), 1 false alarm (fp).
    cm = Confusion(tp=2, fp=1, fn=1, tn=1)
    assert cm.precision == 2 / 3          # tp/(tp+fp)
    assert cm.recall == 2 / 3             # tp/(tp+fn) -> the miss drags recall below 1
    assert math.isclose(cm.f1, 2 / 3)
    assert cm.accuracy == 3 / 5


def test_perfect_gate_scores_one():
    cm = Confusion(tp=5, fp=0, fn=0, tn=2)
    assert cm.precision == 1.0 and cm.recall == 1.0 and cm.f1 == 1.0


def test_missed_defect_tanks_recall_not_precision():
    # A gate that flags nothing: every defect slips through.
    cm = confusion_from_labels([(False, True), (False, True), (True, True)])
    assert cm.recall == 0.0          # caught 0 of 2 defects
    assert cm.precision == 0.0       # no positive predictions -> defined as 0.0
    assert cm.tn == 1


def test_zero_denominator_guards_return_zero():
    empty = Confusion()
    assert empty.precision == 0.0 and empty.recall == 0.0
    assert empty.f1 == 0.0 and empty.accuracy == 0.0 and empty.support == 0
