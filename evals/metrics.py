"""Pure classification metrics for the seeded-defect evals (no I/O, no DB).

The Verifier is a *defect gate*, so we evaluate it as a binary classifier where
the **positive class is "defective"** (the artifact should FAIL the criterion).
That framing makes the two numbers that matter read naturally:

- **recall** = of the truly-defective artifacts, how many the gate CAUGHT
  (a miss — ``fn`` — is a defect that slipped through: the dangerous error);
- **precision** = of the artifacts the gate FLAGGED, how many were truly defective
  (a false alarm — ``fp`` — is good work the gate wrongly rejected).

Everything here is deliberately pure so the arithmetic is unit-testable without a
database, a model, or a filesystem.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Tuple


@dataclass(frozen=True)
class Confusion:
    """A 2x2 confusion matrix with the positive class = "defective".

    - ``tp`` — expected fail, predicted fail  (defect correctly CAUGHT)
    - ``fp`` — expected pass, predicted fail  (false alarm on good work)
    - ``fn`` — expected fail, predicted pass  (defect MISSED — slipped through)
    - ``tn`` — expected pass, predicted pass  (good work correctly passed)
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def support(self) -> int:
        """Total labeled cases."""
        return self.tp + self.fp + self.fn + self.tn

    @property
    def positives(self) -> int:
        """Truly-defective cases (the positive class)."""
        return self.tp + self.fn

    @property
    def precision(self) -> float:
        """TP / (TP + FP). 0.0 when nothing was flagged (no positive predictions)."""
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        """TP / (TP + FN). 0.0 when there are no defective cases to catch."""
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall (0.0 when both are 0)."""
        p, r = self.precision, self.recall
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        """(TP + TN) / support (fraction of correct verdicts overall)."""
        return (self.tp + self.tn) / self.support if self.support else 0.0

    def to_dict(self) -> dict:
        """Matrix counts + derived rates, rounded for a stable report."""
        d = asdict(self)
        d.update(
            support=self.support,
            positives=self.positives,
            precision=round(self.precision, 4),
            recall=round(self.recall, 4),
            f1=round(self.f1, 4),
            accuracy=round(self.accuracy, 4),
        )
        return d


def confusion_from_labels(rows: Iterable[Tuple[bool, bool]]) -> Confusion:
    """Build a :class:`Confusion` from ``(expected_pass, predicted_pass)`` rows.

    Both flags are the *pass* verdict (True = "meets the criterion"); the positive
    class for the matrix is the negation (defective = did NOT pass), so a defect the
    gate correctly fails is a true positive. This is the single place the pass/fail
    booleans are mapped to the matrix, so the eval and its tests agree by
    construction.
    """
    tp = fp = fn = tn = 0
    for expected_pass, predicted_pass in rows:
        expected_defect = not expected_pass
        predicted_defect = not predicted_pass
        if expected_defect and predicted_defect:
            tp += 1
        elif not expected_defect and predicted_defect:
            fp += 1
        elif expected_defect and not predicted_defect:
            fn += 1
        else:
            tn += 1
    return Confusion(tp=tp, fp=fp, fn=fn, tn=tn)
