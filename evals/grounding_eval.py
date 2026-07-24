"""Grounding/fabrication telemetry eval — is quality MEASURABLE? (S3, ADR-0021).

The stakeholder's doctrine is blunt: everything relayed to the human must be
grounded, the Spokesman is ultimately accountable, and fabrication is the worst
offense. This eval proves the *measurement layer* for that doctrine works: it SEEDS
a KNOWN comms/trust shape directly into the S1 accountability ledger
(``comms_claims`` + ``identity_trust``) under throwaway ``eval-grounding-*``
identities, runs the REAL telemetry (:func:`runtime.quality.grounding_report`), and
asserts it recovers that shape exactly — with the fabrication-rate reported as a
proper proportion carrying ``n`` + a Wilson 95% CI + the small-sample flag
(:mod:`evals.stats`), mirroring every other rate in harness v2.

Known seeded shape: **5 verified + 1 rejected (a fabrication) + 1 unverifiable + 1
pending**, one fabrication strike (→ 1 revoked identity), plus 1 quarantined
identity. So: checked=7, fabrication_rate=1/7, verified_rate=5/7, revoked=1,
quarantined=1, total_strikes=1, one top offender.

HONEST SCOPE (mirrors the other evals): this measures the LEDGER/TELEMETRY mechanism
— that recorded fabrications are counted and rated correctly. Whether the Spokesman
gate + real models actually CATCH fabrication end-to-end is measured once those land
(S2 gate). Seeded rows are namespaced ``eval-grounding-*`` and deleted after the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

from runtime.grounding import EvidenceKind, EvidenceRef
from runtime.quality import grounding_report
from runtime.trust import (
    STRIKE_FABRICATION,
    VERIFICATION_REJECTED,
    VERIFICATION_UNVERIFIABLE,
    VERIFICATION_VERIFIED,
    record_claim,
    record_strike,
    set_claim_verification,
)

from .stats import Rate, rate

#: The KNOWN seeded shape this eval asserts the telemetry recovers exactly.
EXPECTED = {
    "verified": 5,
    "rejected": 1,      # a fabrication
    "unverifiable": 1,
    "pending": 1,
    "checked": 7,       # verified + rejected + unverifiable (pending excluded)
    "total_claims": 8,
    "distinct_identities": 4,
    "revoked_identities": 1,
    "quarantined_identities": 1,
    "total_strikes": 1,
}


def seed_grounding_shape(conn: Any, prefix: str) -> None:
    """Seed the KNOWN comms/trust shape under the throwaway ``prefix`` identities."""
    def _ev() -> EvidenceRef:
        return EvidenceRef(kind=EvidenceKind.TASK, locator=f"task:{uuid4().hex[:8]}")

    def _claim(identity: str, statement: str, status: Optional[str]):
        cid = record_claim(conn, originating_identity=identity, statement=statement,
                           evidence=[_ev()])
        if status is not None:
            set_claim_verification(conn, cid, status, verified_by="spokesman",
                                   reason="eval")
        return cid

    verifier = f"{prefix}-verifier"
    for i in range(EXPECTED["verified"]):
        _claim(verifier, f"verified claim {i}", VERIFICATION_VERIFIED)

    fabricator = f"{prefix}-fabricator"
    cid = _claim(fabricator, "a fabricated claim", VERIFICATION_REJECTED)
    # Zero-tolerance: one fabrication strike permanently revokes the identity.
    record_strike(conn, fabricator, claim_id=cid, kind=STRIKE_FABRICATION)

    _claim(f"{prefix}-honest", "couldn't confirm this", VERIFICATION_UNVERIFIABLE)
    _claim(f"{prefix}-pending", "not checked yet", None)

    # A quarantined identity (the guarded writer produces trusted/revoked only; a
    # quarantine is set out-of-band, so seed it directly for the counter).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO identity_trust (identity, trust_state, human_relay_allowed) "
            "VALUES (%s, 'quarantined', false)",
            (f"{prefix}-quarantined",),
        )
    conn.commit()


def cleanup_grounding_shape(conn: Any, prefix: str) -> None:
    """Delete every throwaway row seeded under ``prefix`` (comms + trust)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM comms_claims WHERE originating_identity LIKE %s",
                    (f"{prefix}%",))
        cur.execute("DELETE FROM identity_trust WHERE identity LIKE %s",
                    (f"{prefix}%",))
    conn.commit()


@dataclass
class GroundingEvalResult:
    """Outcome of the grounding-telemetry eval: the recovered report + pass checks."""

    prefix: str
    report: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)

    def rates(self) -> list[Rate]:
        """The verification proportions as :class:`~evals.stats.Rate`s — each with
        ``n`` + Wilson 95% CI + small-``n`` flag, exactly like every other harness
        rate. ``n`` = the CHECKED claims (verified + rejected + unverifiable)."""
        v = self.report.get("verification", {})
        checked = int(v.get("checked", 0))
        return [
            rate("comms_verified_rate", int(v.get("verified", 0)), checked),
            rate("comms_fabrication_rate", int(v.get("rejected", 0)), checked),
        ]

    @property
    def passed(self) -> bool:
        """True iff every seeded-shape check matched the telemetry (mechanism proof)."""
        return all(c["ok"] for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "name": "grounding_fabrication_telemetry",
            "description": (
                "Seed a KNOWN comms/trust shape into the S1 ledger and assert "
                "runtime.quality.grounding_report recovers it — fabrication-rate "
                "reported with n + Wilson 95% CI. Measures the LEDGER/TELEMETRY "
                "mechanism; end-to-end fabrication-catch quality lands with the "
                "Spokesman gate + real models."
            ),
            "prefix": self.prefix,
            "rates": [r.to_dict() for r in self.rates()],
            "checks": self.checks,
            "counts": self.report.get("counts"),
            "verification": {
                k: self.report.get("verification", {}).get(k)
                for k in ("verified", "rejected", "unverifiable", "pending", "checked")
            },
            "top_offenders": self.report.get("top_offenders"),
            "passed": self.passed,
        }


def _check(label: str, got: Any, want: Any) -> dict:
    return {"check": label, "got": got, "want": want, "ok": got == want}


def run_grounding_eval(conn: Any, *, keep: bool = False) -> GroundingEvalResult:
    """Seed the known shape, run the real telemetry, assert recovery, then clean up.

    Needs a live ``conn`` (writes + reads the S1 ledger). Rows are namespaced
    ``eval-grounding-*`` and deleted afterward unless ``keep=True``."""
    prefix = f"eval-grounding-{uuid4().hex[:8]}"
    try:
        seed_grounding_shape(conn, prefix)
        report = grounding_report(conn, identity_prefix=prefix)
        v, c = report["verification"], report["counts"]
        checks = [
            _check("verified", v["verified"], EXPECTED["verified"]),
            _check("rejected", v["rejected"], EXPECTED["rejected"]),
            _check("unverifiable", v["unverifiable"], EXPECTED["unverifiable"]),
            _check("pending", v["pending"], EXPECTED["pending"]),
            _check("checked", v["checked"], EXPECTED["checked"]),
            _check("total_claims", c["total_claims"], EXPECTED["total_claims"]),
            _check("distinct_identities", c["distinct_identities"],
                   EXPECTED["distinct_identities"]),
            _check("revoked_identities", c["revoked_identities"],
                   EXPECTED["revoked_identities"]),
            _check("quarantined_identities", c["quarantined_identities"],
                   EXPECTED["quarantined_identities"]),
            _check("total_strikes", c["total_strikes"], EXPECTED["total_strikes"]),
            _check("fabrication_rate", v["fabrication_rate"]["rate"],
                   round(EXPECTED["rejected"] / EXPECTED["checked"], 4)),
            _check("top_offender",
                   [o["identity"] for o in report["top_offenders"]],
                   [f"{prefix}-fabricator"]),
        ]
        return GroundingEvalResult(prefix=prefix, report=report, checks=checks)
    finally:
        if not keep:
            cleanup_grounding_shape(conn, prefix)


__all__ = [
    "EXPECTED",
    "GroundingEvalResult",
    "seed_grounding_shape",
    "cleanup_grounding_shape",
    "run_grounding_eval",
]
