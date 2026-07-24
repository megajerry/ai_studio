"""Grounding/fabrication telemetry eval tests.

A pure test covers the rate projection (n + Wilson CI) and the pass gate on a
synthetic report (no DB). The live-DB test seeds the KNOWN comms/trust shape into
the S1 ledger, runs the real telemetry via :func:`run_grounding_eval`, asserts it
recovers the shape exactly, and confirms the throwaway rows are cleaned up.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from evals.grounding_eval import (
    EXPECTED,
    GroundingEvalResult,
    run_grounding_eval,
    seed_grounding_shape,
)
from evals.stats import wilson_interval
from runtime import db


def _synthetic_report() -> dict:
    return {
        "scope": "identity_prefix='x'",
        "verification": {"verified": 5, "rejected": 1, "unverifiable": 1,
                         "pending": 1, "checked": 7},
        "counts": {"revoked_identities": 1, "quarantined_identities": 1,
                   "total_strikes": 1},
        "top_offenders": [{"identity": "x-fabricator", "fabrications": 1}],
    }


def test_rates_carry_n_and_wilson_ci():
    r = GroundingEvalResult(prefix="x", report=_synthetic_report())
    rates = {rt.label: rt for rt in r.rates()}
    fr = rates["comms_fabrication_rate"]
    # fabrication-rate = rejected/checked = 1/7, over the CHECKED denominator, with
    # its Wilson 95% CI and the small-sample flag (7 < 30) — never a bare point.
    assert fr.numerator == 1 and fr.n == 7
    assert fr.value == round(1 / 7, 4)
    assert fr.ci == wilson_interval(1, 7)
    assert fr.insufficient is True
    vr = rates["comms_verified_rate"]
    assert vr.numerator == 5 and vr.n == 7 and vr.value == round(5 / 7, 4)


def test_passed_gate_reflects_checks():
    ok = GroundingEvalResult(prefix="x", checks=[{"ok": True}, {"ok": True}])
    bad = GroundingEvalResult(prefix="x", checks=[{"ok": True}, {"ok": False}])
    assert ok.passed is True and bad.passed is False


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_run_grounding_eval_recovers_known_shape_and_cleans_up():
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)

        result = run_grounding_eval(conn)
        assert result.passed is True, [c for c in result.checks if not c["ok"]]
        d = result.to_dict()
        assert d["name"] == "grounding_fabrication_telemetry"

        # The recovered shape matches the KNOWN seeded shape exactly.
        v = d["verification"]
        assert v["verified"] == EXPECTED["verified"]
        assert v["rejected"] == EXPECTED["rejected"]
        assert v["checked"] == EXPECTED["checked"]
        assert d["counts"]["revoked_identities"] == EXPECTED["revoked_identities"]
        assert d["counts"]["quarantined_identities"] == EXPECTED["quarantined_identities"]

        # Fabrication-rate is reported with n + Wilson CI + flag.
        fr = next(r for r in d["rates"] if r["label"] == "comms_fabrication_rate")
        assert fr["n"] == EXPECTED["checked"]
        assert fr["value"] == round(EXPECTED["rejected"] / EXPECTED["checked"], 4)
        assert fr["ci95"] == [round(x, 4) for x in wilson_interval(1, 7)]
        assert fr["insufficient_sample"] is True

        # Cleanup ran: no throwaway rows remain under the seeded prefix.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM comms_claims "
                        "WHERE originating_identity LIKE %s", (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM identity_trust "
                        "WHERE identity LIKE %s", (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == 0
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_keep_flag_leaves_rows_then_manual_cleanup():
    from evals.grounding_eval import cleanup_grounding_shape
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)
        result = run_grounding_eval(conn, keep=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM comms_claims "
                        "WHERE originating_identity LIKE %s", (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == EXPECTED["total_claims"]
        conn.commit()
    finally:
        cleanup_grounding_shape(conn, result.prefix)
        conn.close()
