"""The typed grounding contract for human-facing comms (ADR-0021).

Everything the studio says to the human must be grounded in verifiable evidence.
This module defines the *shape* of that contract — nothing else. It is pure
(stdlib + pydantic only; NO DB, NO verification logic) so it can be imported by
BOTH the runtime (:mod:`runtime.trust`) and the Spokesman without pulling in
psycopg or any I/O.

Two types:

- :class:`EvidenceRef` — a typed, checkable pointer to a piece of evidence (an
  event, a task, a DB row, an artifact, a file:line, or a metric). Evidence is
  always a *reference the verifier can resolve*, never inlined prose.
- :class:`Claim` — one human-facing assertion, either a **factual claim** (which
  MUST carry ≥1 evidence ref) or a **judgment** (an opinion/recommendation, which
  may have none but must be labelled ``is_judgment=True``).

The single hard rule enforced here: a non-judgment claim with **empty evidence is
invalid**. Verifying whether the referenced evidence actually *supports* the
statement is the Spokesman gate's job (S2/S3), not this module's.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class EvidenceKind(str, Enum):
    """The kinds of evidence a claim may reference (ADR-0021, ADR-0014 hierarchy).

    String-valued so it round-trips cleanly through the ``comms_claims.evidence``
    JSONB column and event payloads.
    """

    EVENT = "event"        # an append-only event — locator: its seq or id
    TASK = "task"          # a task row — locator: the task id
    DB_ROW = "db_row"      # any DB row — locator: "table:pk"
    ARTIFACT = "artifact"  # a produced artifact — locator: its content hash
    FILE = "file"          # a source location — locator: "path:line"
    METRIC = "metric"      # a measured metric — locator: the metric query


#: The closed vocabulary of evidence kinds, as raw strings (for cheap membership
#: checks without importing the enum).
EVIDENCE_KINDS: frozenset[str] = frozenset(k.value for k in EvidenceKind)


class EvidenceRef(BaseModel):
    """A typed, resolvable pointer to one piece of evidence.

    ``kind`` is one of :class:`EvidenceKind`; ``locator`` identifies the referent
    within that kind (an event seq/id, a task id, ``"table:pk"``, an artifact hash,
    ``"file:line"``, or a metric query). ``expected`` optionally records what the
    verifier should find there (e.g. the value/status the statement asserts), so
    resolution can confirm the evidence *supports* the claim rather than merely
    existing. This is a pure data contract — it does no resolution itself.
    """

    kind: EvidenceKind
    locator: str
    expected: Optional[str] = None

    @model_validator(mode="after")
    def _non_empty_locator(self) -> "EvidenceRef":
        if not self.locator or not str(self.locator).strip():
            raise ValueError("EvidenceRef.locator must be non-empty")
        return self


class Claim(BaseModel):
    """One human-facing assertion + the evidence that grounds it (ADR-0021).

    A **factual claim** (``is_judgment=False``, the default) asserts something
    about studio state and MUST carry at least one :class:`EvidenceRef` — an
    unbacked factual claim is invalid and not sendable as fact. A **judgment**
    (``is_judgment=True``) is an opinion / recommendation / plan; it may carry no
    evidence but is labelled so it is never dressed up as verified fact.

    Validation here is structural only (a non-judgment claim needs evidence).
    Whether the evidence actually supports the statement is decided later by the
    Spokesman verify-or-request-proof gate (S2/S3).
    """

    statement: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    is_judgment: bool = False

    @model_validator(mode="after")
    def _factual_claims_need_evidence(self) -> "Claim":
        if not self.statement or not self.statement.strip():
            raise ValueError("Claim.statement must be non-empty")
        if not self.is_judgment and not self.evidence:
            raise ValueError(
                "a non-judgment (factual) Claim must carry at least one EvidenceRef; "
                "either supply evidence or mark it is_judgment=True"
            )
        return self

    def evidence_payload(self) -> list[dict]:
        """The evidence as a plain JSON-serializable list (for the ``evidence`` column)."""
        return [e.model_dump(mode="json") for e in self.evidence]


__all__ = [
    "EvidenceKind",
    "EVIDENCE_KINDS",
    "EvidenceRef",
    "Claim",
]
