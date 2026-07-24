"""Live-DB tests for the capacity telemetry (C2, ADR-0022).

Seed budgets + real ``model.call`` spend at KNOWN fractions under a throwaway
``capws-*`` workstream prefix, then assert :func:`runtime.quality.capacity_report`
recovers the exact per-allocation zone, spent-vs-cap, reserve headroom, burn rate,
and burn-projected exhaustion (``projected_breach``), plus the studio-wide roll-up
(zone counts, projected breaches, and ``at_risk_rate`` with its Wilson 95% CI +
small-sample flag). Also None-safe/empty behavior, org-ceiling utilization, and that
``quality_report`` carries the new ``capacity_global`` section without disturbing the
existing sections. SKIP cleanly when no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_quality_capacity_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import budget, db
from runtime.migrate import migrate
from runtime.quality import capacity_report, quality_report

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0010_budgets + 0013_capacity_governance applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def pfx() -> str:
    """A unique throwaway workstream prefix so each test asserts an exact shape."""
    return f"capws-{uuid4().hex[:12]}"


# --- seeding helpers --------------------------------------------------------


def _seed_call(conn, ws, *, cost_usd=0.0, input_tokens=0, output_tokens=0):
    """Append one real model.call event (the same cost source budgets read)."""
    from runtime.events import append_event
    from runtime.models import make_event

    append_event(
        conn,
        make_event(
            workstream=ws,
            type="model.call",
            payload={
                "model": "m", "provider": "dryrun", "role": "exec",
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cached_tokens": 0, "cost_usd": cost_usd, "latency_ms": 1,
            },
        ),
    )


#: KNOWN token-cap shape (cap 1000 tokens; fracs 0.70/0.85/0.90) → one workstream
#: per target zone, plus an idle (budgeted, no spend) workstream for the
#: projected-breach contrast. spent chosen to land squarely inside each zone.
_SHAPE = {
    "ok":       500,   # 0.50 < warn
    "warn":     750,   # 0.75 in [warn, throttle)
    "throttle": 860,   # 0.86 in [throttle, reserve)
    "reserve":  920,   # 0.92 >= reserve (headroom 80)
    "over":    1200,   # > cap
    "idle":       0,   # budgeted but no spend → no burn → not a projected breach
}


def _seed_shape(conn, pfx: str) -> None:
    for zone, tokens in _SHAPE.items():
        ws = f"{pfx}-{zone}"
        budget.set_budget(
            conn, ws, cap_tokens=1000,
            warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
        )
        if tokens:
            _seed_call(conn, ws, input_tokens=tokens)


def _by_ws(rep: dict) -> dict:
    return {e["workstream"]: e for e in rep["by_workstream"]}


# --- exact zones + spent + reserve headroom ---------------------------------


def test_known_zones_spent_and_reserve_headroom(conn, pfx):
    _seed_shape(conn, pfx)
    rep = capacity_report(conn, workstream_prefix=pfx)

    assert rep["workstreams_budgeted"] == 6
    assert rep["allocations_scored"] == 6
    entries = _by_ws(rep)

    # Every allocation lands in exactly the seeded zone, with the seeded spend.
    for zone, tokens in _SHAPE.items():
        e = entries[f"{pfx}-{zone}"]
        assert e["zone"] == zone if zone != "idle" else e["zone"] == "ok"
        assert e["cap_tokens"] == 1000 and e["cap_usd"] is None
        assert e["spent_tokens"] == tokens
        assert e["spent_frac"] == round(tokens / 1000, 6)
        # Token cap only → no USD headroom reported.
        assert e["reserve_headroom_usd"] is None

    # Reserve headroom = the buffer of tokens still spendable before the hard cap.
    assert entries[f"{pfx}-reserve"]["reserve_headroom_tokens"] == 80   # 1000-920
    assert entries[f"{pfx}-throttle"]["reserve_headroom_tokens"] == 140  # 1000-860
    # 'over' is past the cap → the buffer is negative (already breached).
    assert entries[f"{pfx}-over"]["reserve_headroom_tokens"] == -200
    assert entries[f"{pfx}-over"]["remaining_tokens"] == -200


# --- burn + projected-breach flag (hand-calculable, time-independent) -------


def test_burn_and_projected_breach(conn, pfx):
    _seed_shape(conn, pfx)
    entries = _by_ws(capacity_report(conn, workstream_prefix=pfx))

    # 'ok': one seeded call of 500 tokens → tokens_per_call=500, remaining=500 →
    # calls_to_exhaustion = 500/500 = 1.0 (deterministic; independent of wall-clock).
    ok = entries[f"{pfx}-ok"]
    assert ok["burn"]["calls"] == 1
    assert ok["burn"]["tokens_per_call"] == 500.0
    assert ok["projection"]["calls_to_exhaustion"] == 1.0
    assert ok["projection"]["projected_breach"] is True  # on track to breach at pace

    # 'idle': budgeted but no spend → zero burn → no projection → NOT a breach.
    idle = entries[f"{pfx}-idle"]
    assert idle["burn"]["calls"] == 0
    assert idle["projection"]["calls_to_exhaustion"] is None
    assert idle["projection"]["minutes_to_exhaustion"] is None
    assert idle["projection"]["projected_breach"] is False

    # 'over' is already past the cap → flagged a (current) breach regardless of burn.
    assert entries[f"{pfx}-over"]["projection"]["projected_breach"] is True


# --- studio-wide roll-up (zone counts + at_risk_rate with n + Wilson CI) ----


def test_rollup_zone_counts_and_at_risk_rate_ci(conn, pfx):
    from runtime.quality import wilson_interval

    _seed_shape(conn, pfx)
    rep = capacity_report(conn, workstream_prefix=pfx)
    roll = rep["rollup"]

    # ok=2 (the ok + the idle), warn/throttle/reserve/over = 1 each.
    assert roll["zone_counts"] == {
        "ok": 2, "warn": 1, "throttle": 1, "reserve": 1, "over": 1,
    }
    # Every allocation with spend is on track to breach; only the idle one is not.
    assert roll["projected_breaches"] == 5

    # at_risk_rate = allocations NOT in 'ok' / total = 4/6, carrying n + Wilson CI.
    ar = roll["at_risk_rate"]
    assert ar["successes"] == 4 and ar["n"] == 6
    assert ar["rate"] == round(4 / 6, 4)
    assert ar["ci95"] == wilson_interval(4, 6)
    assert ar["insufficient_sample"] is True  # 6 < 30
    lo, hi = ar["ci95"]
    assert 0.0 < lo <= ar["rate"] <= hi < 1.0  # an honest interval, not a bare point


# --- None-safe / empty ------------------------------------------------------


def test_no_budget_prefix_is_none_safe(conn):
    empty = f"capws-empty-{uuid4().hex[:8]}"
    rep = capacity_report(conn, workstream_prefix=empty)
    assert rep["workstreams_budgeted"] == 0
    assert rep["allocations_scored"] == 0
    assert rep["by_workstream"] == []
    assert rep["org_ceiling"] is None
    roll = rep["rollup"]
    assert roll["zone_counts"] == {z: 0 for z in ("ok", "warn", "throttle",
                                                  "reserve", "over")}
    assert roll["projected_breaches"] == 0
    ar = roll["at_risk_rate"]
    assert ar["rate"] is None and ar["ci95"] is None
    assert ar["n"] == 0 and ar["successes"] == 0
    assert ar["insufficient_sample"] is True  # no sample → untrustworthy


def test_workstream_with_spend_but_no_budget_is_excluded(conn, pfx):
    # A workstream that spent but has NO budget row is unconstrained → not reported.
    ws = f"{pfx}-nobudget"
    _seed_call(conn, ws, input_tokens=999)
    rep = capacity_report(conn, workstream_prefix=pfx)
    assert ws not in _by_ws(rep)
    assert rep["allocations_scored"] == 0


def test_tracking_only_row_without_cap_is_skipped(conn, pfx):
    # A budget row with NO cap set (tracking intent only) has nothing to gate → skipped.
    ws = f"{pfx}-tracking"
    budget.set_budget(conn, ws)  # cap_usd and cap_tokens both None
    rep = capacity_report(conn, workstream_prefix=pfx)
    assert ws not in _by_ws(rep)
    assert rep["allocations_scored"] == 0


# --- org ceiling utilization ------------------------------------------------


def test_org_ceiling_utilization(conn):
    # The org sentinel's spend is the org-wide total; utilization = spend/cap.
    org_spent = budget.org_spent(conn, period="daily").cost_usd
    org_cap = org_spent + 10.0
    budget.set_budget(conn, budget.ORG_WORKSTREAM, period="daily", cap_usd=org_cap)
    try:
        rep = capacity_report(conn)  # global (no prefix) → org_ceiling present
        assert rep["org_ceiling"] is not None
        daily = [o for o in rep["org_ceiling"] if o["period"] == "daily"]
        assert len(daily) == 1
        o = daily[0]
        # cap_usd round-trips through numeric → compare with a FP tolerance.
        assert o["cap_usd"] == pytest.approx(org_cap)
        assert o["spent_usd"] == pytest.approx(round(org_spent, 6))
        assert o["utilization_usd"] == pytest.approx(org_spent / org_cap, abs=1e-6)
    finally:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("DELETE FROM budgets WHERE workstream = %s",
                            (budget.ORG_WORKSTREAM,))


# --- integration into quality_report (no regression) ------------------------


def test_quality_report_includes_capacity_section(conn, pfx):
    _seed_shape(conn, pfx)
    rep = quality_report(conn, f"{pfx}-ok")
    # Existing sections are preserved.
    assert {"totals", "rates", "cost", "latency", "by_model_global",
            "pm_decision_quality", "grounding_global"} <= set(rep)
    # New capacity section is present and global (spans all budgeted workstreams,
    # not ws-filtered): the seeded workstreams appear even though we scoped the
    # rollup to one workstream.
    assert "capacity_global" in rep
    cap = rep["capacity_global"]
    assert set(cap) == {"workstreams_budgeted", "allocations_scored",
                        "by_workstream", "org_ceiling", "rollup"}
    seeded = {e["workstream"] for e in cap["by_workstream"]}
    assert {f"{pfx}-ok", f"{pfx}-over"} <= seeded
