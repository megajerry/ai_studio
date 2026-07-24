"""Pure (DB-free) tests for the grounding contract (ADR-0021).

Covers the single structural rule the typed contract enforces: a non-judgment
(factual) :class:`~runtime.grounding.Claim` MUST carry evidence; a judgment need
not; and an :class:`~runtime.grounding.EvidenceRef` needs a real locator + kind.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from runtime.grounding import (
    EVIDENCE_KINDS,
    Claim,
    EvidenceKind,
    EvidenceRef,
)


def test_evidence_kinds_are_the_documented_closed_set():
    assert EVIDENCE_KINDS == {
        "event", "task", "db_row", "artifact", "file", "metric",
    }


def test_evidence_ref_roundtrips_and_serializes():
    ref = EvidenceRef(kind=EvidenceKind.EVENT, locator="seq:42", expected="merged")
    assert ref.kind is EvidenceKind.EVENT
    dumped = ref.model_dump(mode="json")
    assert dumped == {"kind": "event", "locator": "seq:42", "expected": "merged"}


def test_evidence_ref_rejects_empty_locator():
    with pytest.raises(ValidationError):
        EvidenceRef(kind=EvidenceKind.TASK, locator="   ")


def test_evidence_ref_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        EvidenceRef(kind="rumor", locator="x")  # type: ignore[arg-type]


def test_factual_claim_requires_evidence():
    """A non-judgment claim with EMPTY evidence is invalid (the core rule)."""
    with pytest.raises(ValidationError):
        Claim(statement="task X merged", evidence=[])


def test_factual_claim_with_evidence_is_valid():
    claim = Claim(
        statement="task X merged",
        evidence=[EvidenceRef(kind=EvidenceKind.TASK, locator="task:abc")],
    )
    assert claim.is_judgment is False
    assert len(claim.evidence) == 1
    assert claim.evidence_payload() == [
        {"kind": "task", "locator": "task:abc", "expected": None}
    ]


def test_judgment_may_have_no_evidence():
    """A judgment is allowed with no evidence — but must be labelled as such."""
    claim = Claim(statement="I think we should ship Tuesday", is_judgment=True)
    assert claim.is_judgment is True
    assert claim.evidence == []


def test_claim_rejects_empty_statement():
    with pytest.raises(ValidationError):
        Claim(statement="   ", is_judgment=True)
