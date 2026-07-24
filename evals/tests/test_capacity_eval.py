"""Capacity telemetry eval tests.

A pure test covers the at-risk rate projection (n + Wilson CI) and the pass gate on a
synthetic report (no DB). The live-DB test seeds the KNOWN budget + spend shape, runs
the real telemetry via :func:`run_capacity_eval`, asserts it recovers the shape
exactly, and confirms the throwaway rows are cleaned up.
"""

from __future__ import annotations

import pytest

from evals.capacity_eval import (
    EXPECTED,
    CapacityEvalResult,
    cleanup_capacity_shape,
    run_capacity_eval,
)
from evals.stats import wilson_interval
from runtime import db


def _synthetic_report() -> dict:
    """A capacity_report-shaped dict with the KNOWN zone counts (no DB)."""
    return {
        "allocations_scored": EXPECTED["n"],
        "by_workstream": [],
        "rollup": {
            "zone_counts": EXPECTED["zone_counts"],
            "projected_breaches": EXPECTED["projected_breaches"],
            "at_risk_rate": {"rate": round(EXPECTED["at_risk"] / EXPECTED["n"], 4),
                             "successes": EXPECTED["at_risk"], "n": EXPECTED["n"]},
        },
    }


def test_rates_carry_n_and_wilson_ci():
    r = CapacityEvalResult(prefix="x", report=_synthetic_report())
    rates = {rt.label: rt for rt in r.rates()}
    ar = rates["capacity_at_risk_rate"]
    # at_risk_rate = not-ok / total = 4/6, with its Wilson 95% CI and small-sample
    # flag (6 < 30) — never a bare point.
    assert ar.numerator == EXPECTED["at_risk"] and ar.n == EXPECTED["n"]
    assert ar.value == round(EXPECTED["at_risk"] / EXPECTED["n"], 4)
    assert ar.ci == wilson_interval(EXPECTED["at_risk"], EXPECTED["n"])
    assert ar.insufficient is True


def test_passed_gate_reflects_checks():
    ok = CapacityEvalResult(prefix="x", checks=[{"ok": True}, {"ok": True}])
    bad = CapacityEvalResult(prefix="x", checks=[{"ok": True}, {"ok": False}])
    assert ok.passed is True and bad.passed is False


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_run_capacity_eval_recovers_known_shape_and_cleans_up():
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)

        result = run_capacity_eval(conn)
        assert result.passed is True, [c for c in result.checks if not c["ok"]]
        d = result.to_dict()
        assert d["name"] == "capacity_telemetry"

        # The recovered shape matches the KNOWN seeded shape exactly.
        assert d["allocations_scored"] == EXPECTED["allocations"]
        assert d["zone_counts"] == EXPECTED["zone_counts"]
        assert d["projected_breaches"] == EXPECTED["projected_breaches"]

        # at_risk_rate is reported with n + Wilson CI + flag.
        ar = next(r for r in d["rates"] if r["label"] == "capacity_at_risk_rate")
        assert ar["n"] == EXPECTED["n"]
        assert ar["value"] == round(EXPECTED["at_risk"] / EXPECTED["n"], 4)
        assert ar["ci95"] == [round(x, 4)
                              for x in wilson_interval(EXPECTED["at_risk"], EXPECTED["n"])]
        assert ar["insufficient_sample"] is True

        # Cleanup ran: no throwaway rows remain under the seeded prefix.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM budgets WHERE workstream LIKE %s",
                        (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT count(*) AS n FROM events WHERE workstream LIKE %s",
                        (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == 0
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_keep_flag_leaves_rows_then_manual_cleanup():
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)
        result = run_capacity_eval(conn, keep=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM budgets WHERE workstream LIKE %s",
                        (f"{result.prefix}%",))
            assert cur.fetchone()["n"] == EXPECTED["allocations"]
        conn.commit()
    finally:
        cleanup_capacity_shape(conn, result.prefix)
        conn.close()
