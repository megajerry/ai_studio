"""Live-DB tests for the grounding/fabrication telemetry (S3, ADR-0021).

Seed a KNOWN comms/trust shape under a throwaway ``grnd-*`` identity prefix, then
assert :func:`runtime.quality.grounding_report` computes the exact verification
counts, the verified/fabrication rates (each with its Wilson 95% CI + small-sample
flag), the revoked/quarantined identity counts + total strikes, and the top
offenders — plus None-safe/empty behavior and that ``quality_report`` carries the
new ``grounding_global`` section without disturbing the existing sections. SKIP
cleanly when no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_quality_grounding_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.grounding import EvidenceKind, EvidenceRef
from runtime.migrate import migrate
from runtime.quality import grounding_report, quality_report, wilson_interval
from runtime.trust import (
    STRIKE_FABRICATION,
    VERIFICATION_REJECTED,
    VERIFICATION_UNVERIFIABLE,
    VERIFICATION_VERIFIED,
    record_claim,
    record_strike,
    set_claim_verification,
)

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0012 (comms_claims + identity_trust) applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def pfx() -> str:
    """A unique throwaway identity prefix so each test asserts an exact shape."""
    return f"grnd-{uuid4().hex[:12]}"


# --- seeding helpers --------------------------------------------------------


def _ev(locator: str) -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.TASK, locator=locator)


def _claim(conn, identity: str, statement: str, status: str | None):
    """Record one claim from ``identity`` and (optionally) set its verdict."""
    cid = record_claim(conn, originating_identity=identity, statement=statement,
                       evidence=[_ev(f"task:{uuid4().hex[:8]}")])
    if status is not None:
        set_claim_verification(conn, cid, status, verified_by="spokesman",
                               reason="test")
    return cid


def _seed_known_shape(conn, pfx: str) -> None:
    """5 verified + 1 rejected(fabrication) + 1 unverifiable + 1 pending, plus one
    quarantined identity — a KNOWN shape whose telemetry we assert exactly."""
    verifier = f"{pfx}-verifier"
    for i in range(5):
        _claim(conn, verifier, f"verified claim {i}", VERIFICATION_VERIFIED)

    fabricator = f"{pfx}-fabricator"
    cid = _claim(conn, fabricator, "a fabricated claim", VERIFICATION_REJECTED)
    # Zero-tolerance: a fabrication strike revokes the identity permanently.
    record_strike(conn, fabricator, claim_id=cid, kind=STRIKE_FABRICATION)

    _claim(conn, f"{pfx}-honest", "couldn't confirm this", VERIFICATION_UNVERIFIABLE)
    _claim(conn, f"{pfx}-pending", "not checked yet", None)

    # A quarantined identity (the guarded writer only produces revoked/trusted; a
    # quarantine is set out-of-band, so seed it directly for the counter test).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO identity_trust (identity, trust_state, human_relay_allowed) "
            "VALUES (%s, 'quarantined', false)",
            (f"{pfx}-quarantined",),
        )
    conn.commit()


# --- exact counts + rates + Wilson bounds -----------------------------------


def test_known_shape_exact_counts_rates_and_ci(conn, pfx):
    _seed_known_shape(conn, pfx)
    rep = grounding_report(conn, identity_prefix=pfx)

    v = rep["verification"]
    assert v["verified"] == 5
    assert v["rejected"] == 1
    assert v["unverifiable"] == 1
    assert v["pending"] == 1
    assert v["checked"] == 7  # verified + rejected + unverifiable (pending excluded)

    # verified-rate = 5/7 over the CHECKED claims, with n + Wilson CI + flag.
    vr = v["verified_rate"]
    assert vr["successes"] == 5 and vr["n"] == 7 and vr["rate"] == round(5 / 7, 4)
    assert vr["ci95"] == wilson_interval(5, 7)
    assert vr["insufficient_sample"] is True  # 7 < 30

    # fabrication-rate = rejected/checked = 1/7, with the same denominator + CI.
    fr = v["fabrication_rate"]
    assert fr["successes"] == 1 and fr["n"] == 7 and fr["rate"] == round(1 / 7, 4)
    assert fr["ci95"] == wilson_interval(1, 7)
    lo, hi = fr["ci95"]
    assert 0.0 < lo <= fr["rate"] <= hi < 1.0  # honest interval, not a bare point
    assert fr["insufficient_sample"] is True


def test_known_shape_counts_and_offenders(conn, pfx):
    _seed_known_shape(conn, pfx)
    rep = grounding_report(conn, identity_prefix=pfx)

    counts = rep["counts"]
    assert counts["total_claims"] == 8          # 5 + 1 + 1 + 1 (incl. pending)
    assert counts["distinct_identities"] == 4   # verifier, fabricator, honest, pending
    # Ledger rows under the prefix: the revoked fabricator + the quarantined one.
    assert counts["identities_tracked"] == 2
    assert counts["revoked_identities"] == 1
    assert counts["quarantined_identities"] == 1
    assert counts["total_strikes"] == 1         # one fabrication strike

    # Top offenders: only the fabricator, with exactly one fabricated claim.
    assert rep["top_offenders"] == [
        {"identity": f"{pfx}-fabricator", "fabrications": 1}
    ]


# --- None-safe / empty ------------------------------------------------------


def test_empty_prefix_is_none_safe(conn):
    empty = f"grnd-empty-{uuid4().hex[:8]}"
    rep = grounding_report(conn, identity_prefix=empty)
    v = rep["verification"]
    assert v["verified"] == v["rejected"] == v["unverifiable"] == v["pending"] == 0
    assert v["checked"] == 0
    for key in ("verified_rate", "fabrication_rate"):
        r = v[key]
        assert r["rate"] is None and r["ci95"] is None
        assert r["n"] == 0 and r["successes"] == 0
        assert r["insufficient_sample"] is True  # no sample → untrustworthy
    c = rep["counts"]
    assert c["total_claims"] == 0 and c["distinct_identities"] == 0
    assert c["revoked_identities"] == 0 and c["quarantined_identities"] == 0
    assert c["total_strikes"] == 0
    assert rep["top_offenders"] == []


# --- integration into quality_report (no regression) ------------------------


def test_quality_report_includes_grounding_section(conn):
    ws = f"grnd-ws-{uuid4().hex[:8]}"
    rep = quality_report(conn, ws)
    # Existing sections are preserved.
    assert {"totals", "rates", "cost", "latency", "by_model_global",
            "pm_decision_quality"} <= set(rep)
    # New grounding section is present, global (identity-scoped, not ws-filtered),
    # and equals a standalone global grounding_report call.
    assert "grounding_global" in rep
    g = rep["grounding_global"]
    assert g["scope"] == "global"
    assert set(g) == {"scope", "verification", "counts", "top_offenders"}
    assert g == grounding_report(conn)
