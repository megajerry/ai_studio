"""PM build-vs-buy / agile-adoption operating principle tests (ADR-0026).

Prove the principle is internalized as something the PM OWNS — a prompt disposition
plus a budget-tuned baseline external-research cadence the PM triggers — NOT a
hard-coded cron, and without churn/looping:

- the build-vs-buy + agile-adoption text is present in the PM's composed prompt,
  and the ``compose_role_prompt`` layer is off by default (byte-identical);
- the cadence helper is pure/deterministic, bounded, NEVER zero/off, slowest under a
  tight budget and faster with headroom;
- ``run_pm_tick`` commissions EXACTLY ONE external-research task when due and NONE
  when a recent scan exists (no stacking / double-enqueue — the bound);
- a dispatched research task enqueues nothing (no research-of-research loop);
- a DB failure in the commission degrades gracefully — the pm.tick core still plans.

The pure tests run everywhere; the ``run_pm_tick`` tests need a live DB and SKIP
cleanly when no Postgres is reachable. Keyless/dry-run throughout.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from runtime import adaptive, db
from runtime.adaptive import AdaptiveConfig, pm_research_interval_hours
from runtime.enforce import NullEventSink
from runtime.roles import pm as pm_mod
from runtime.roles.pm import (
    EXTERNAL_RESEARCH_GOAL,
    RESEARCH_ORIGIN_EXTERNAL_SCAN,
    _compose_plan_prompt,
    _maybe_commission_research,
    run_pm_tick,
)
from runtime.roles.prompt import compose_role_prompt
from runtime.roles.researcher import RESEARCH_TASK_TYPE

requires_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


# ===========================================================================
# 1. The build-vs-buy / agile-adoption prompt principle (pure, no DB)
# ===========================================================================


def test_strategy_layer_off_by_default_is_byte_identical():
    base = "You are the studio PM. Goal: g."
    assert compose_role_prompt(base) == base
    assert compose_role_prompt(base, strategy_aware=False) == base


def test_strategy_layer_present_when_opted_in():
    out = compose_role_prompt("BASE-PERSONA", strategy_aware=True)
    assert "BASE-PERSONA" in out
    assert "Build vs. buy" in out
    low = out.lower()
    # The load-bearing ideas: buy/borrow vs reinvent, better paradigm, no churn,
    # reviewable (never auto-adopt).
    for token in ("buy", "borrow", "paradigm", "churn", "evidence", "review"):
        assert token in low, token


def test_pm_composed_plan_prompt_carries_the_principle():
    """The PM's actual composed plan prompt contains the build-vs-buy principle."""
    prompt = _compose_plan_prompt("Ship the thing", None)
    assert "Build vs. buy" in prompt
    low = prompt.lower()
    assert "buy" in low and "borrow" in low and "paradigm" in low and "churn" in low
    # It refines, does not replace, the persona (still a PM plan prompt).
    assert "PLAN" in prompt


# ===========================================================================
# 2. Budget-tuned baseline cadence helper — pure, bounded, NEVER zero/off
# ===========================================================================


def test_cadence_uncapped_budget_is_fastest_baseline():
    cfg = AdaptiveConfig()  # defaults: 24h..168h
    # None (uncapped / unknown) → treat as ample → fastest baseline (the floor).
    assert pm_research_interval_hours(None, config=cfg) == cfg.research_baseline_min_hours


def test_cadence_slowest_under_tight_budget_fastest_with_headroom():
    cfg = AdaptiveConfig()
    tight = pm_research_interval_hours(0.0, config=cfg)   # starved
    ample = pm_research_interval_hours(1.0, config=cfg)   # full headroom
    mid = pm_research_interval_hours(0.5, config=cfg)
    assert ample == cfg.research_baseline_min_hours       # fastest
    assert tight == cfg.research_baseline_max_hours        # slowest
    # More headroom => a shorter (faster) interval, strictly monotone here.
    assert ample < mid < tight


def test_cadence_is_bounded_and_never_zero_or_off():
    cfg = AdaptiveConfig()
    for frac in (0.0, 0.05, 0.2, 0.5, 0.8, 1.0, None):
        hours = pm_research_interval_hours(frac, config=cfg)
        # Never zero, never off (infinite); always within the closed baseline range.
        assert 0.0 < hours
        assert cfg.research_baseline_min_hours <= hours <= cfg.research_baseline_max_hours


def test_cadence_is_deterministic():
    cfg = AdaptiveConfig()
    assert pm_research_interval_hours(0.3, config=cfg) == pm_research_interval_hours(0.3, config=cfg)


def test_cadence_independent_of_adaptive_master_switch(monkeypatch):
    """The baseline cadence is a PM principle — it does NOT require ADAPTIVE_INTENSITY."""
    monkeypatch.delenv("ADAPTIVE_INTENSITY", raising=False)
    off = pm_research_interval_hours(0.5)
    monkeypatch.setenv("ADAPTIVE_INTENSITY", "on")
    on = pm_research_interval_hours(0.5)
    assert off == on  # the master switch does not gate the baseline


def test_cadence_env_overrides_and_guard_bounds(monkeypatch):
    monkeypatch.setenv("ADAPTIVE_RESEARCH_MIN_HOURS", "12")
    monkeypatch.setenv("ADAPTIVE_RESEARCH_MAX_HOURS", "48")
    cfg = AdaptiveConfig.from_env()
    assert cfg.research_baseline_min_hours == 12.0
    assert cfg.research_baseline_max_hours == 48.0
    assert pm_research_interval_hours(1.0, config=cfg) == 12.0
    assert pm_research_interval_hours(0.0, config=cfg) == 48.0


def test_maybe_commission_no_conn_is_noop():
    """No conn (fake-queue unit paths) → the commission is a safe no-op."""
    class _T:
        workstream = "ws"
        priority = 0
    calls = []
    out = _maybe_commission_research(
        None, _T(), enqueue=lambda *a, **k: calls.append(k)
    )
    assert out is None and calls == []


# ===========================================================================
# 3. run_pm_tick owns the trigger — live DB, keyless/dry-run
# ===========================================================================


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.setenv("SEARCH_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover - a network call would be a test bug
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


@pytest.fixture(scope="module")
def conn():
    from runtime.migrate import migrate

    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _good_plan() -> dict:
    return {
        "restated_goal": "Ship the thing",
        "confidence": 0.9,
        "feasible": True,
        "success_criteria": ["shipped and verified"],
        "work_items": [
            {"title": "P1", "type": "work.task", "instructions": "do p1",
             "success_criterion": "p1 done", "marker": "m1"},
            {"title": "P2", "type": "work.task", "instructions": "do p2",
             "success_criterion": "p2 done", "marker": "m2"},
        ],
    }


def _plan_call_model(**kw):
    return type("C", (), {"text": json.dumps(_good_plan())})()


def _count_research(conn, ws) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type = %s",
            (ws, RESEARCH_TASK_TYPE),
        )
        n = cur.fetchone()["n"]
    conn.commit()
    return n


def _backdate_workstream(conn, ws, *, days: int) -> None:
    """Age the workstream's tasks so the 'never scanned → warm up' gate is satisfied."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tasks SET created_at = now() - make_interval(days => %s) "
            "WHERE workstream = %s",
            (days, ws),
        )
    conn.commit()


@requires_db
def test_pm_commissions_exactly_one_scan_when_due_and_none_when_recent(conn):
    from runtime.tasks import enqueue_task

    ws = f"pm-research-{uuid4().hex[:12]}"
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick", payload={"goal": "Ship it"})
    # Age the workstream past the slowest cadence (168h) so the baseline scan is DUE
    # even though it has never been scanned (the warm-up window has elapsed).
    _backdate_workstream(conn, ws, days=30)

    r1 = run_pm_tick(conn, pm_task, NullEventSink(), call_model=_plan_call_model)
    assert r1.decision == "planned"
    # EXACTLY ONE external-research scan was commissioned, and reported on the result.
    assert r1.research_task_id is not None
    assert _count_research(conn, ws) == 1

    # The commissioned task is the studio-level external scan (marked + goal set).
    from runtime.tasks import get_task
    from uuid import UUID
    scan = get_task(conn, UUID(r1.research_task_id))
    assert scan.type == RESEARCH_TASK_TYPE
    assert scan.payload["origin"] == RESEARCH_ORIGIN_EXTERNAL_SCAN
    assert scan.payload["goal"] == EXTERNAL_RESEARCH_GOAL

    # A SECOND tick immediately after must NOT stack a scan — the fresh one is recent
    # (< the cadence interval), so it is not due. Proves the per-due-window bound.
    pm_task2 = enqueue_task(conn, workstream=ws, type="pm.tick", payload={"goal": "Ship it"})
    r2 = run_pm_tick(conn, pm_task2, NullEventSink(), call_model=_plan_call_model)
    assert r2.decision == "planned"
    assert r2.research_task_id is None
    assert _count_research(conn, ws) == 1  # still exactly one — no double-enqueue


@requires_db
def test_fresh_workstream_is_not_scanned_on_first_tick(conn):
    """A brand-new workstream (younger than the cadence interval) is NOT scanned —
    the studio doesn't churn a scan on the very first pulse."""
    from runtime.tasks import enqueue_task

    ws = f"pm-research-fresh-{uuid4().hex[:12]}"
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick", payload={"goal": "Ship it"})
    r = run_pm_tick(conn, pm_task, NullEventSink(), call_model=_plan_call_model)
    assert r.decision == "planned"
    assert r.research_task_id is None
    assert _count_research(conn, ws) == 0


@requires_db
def test_dispatched_scan_enqueues_no_further_tasks(conn):
    """The commissioned scan, when dispatched, enqueues nothing (no research loop)."""
    from runtime.policy import PolicyConfig
    from runtime.capabilities import Capability
    from runtime.roles.researcher import run_research
    from runtime.tasks import enqueue_task, get_task
    from uuid import UUID

    ws = f"pm-research-loop-{uuid4().hex[:12]}"
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick", payload={"goal": "Ship it"})
    _backdate_workstream(conn, ws, days=30)
    r1 = run_pm_tick(conn, pm_task, NullEventSink(), call_model=_plan_call_model)
    assert r1.research_task_id is not None

    before = _total_tasks(conn, ws)
    scan = get_task(conn, UUID(r1.research_task_id))
    net_only = PolicyConfig(roles={"researcher": frozenset({Capability.NET_FETCH})})
    run_research(conn, scan, NullEventSink(), policy=net_only)
    after = _total_tasks(conn, ws)
    assert after == before  # the researcher enqueued nothing


@requires_db
def test_commission_db_failure_degrades_gracefully(conn, monkeypatch):
    """A DB failure inside the commission NEVER crashes the tick — it still plans."""
    from runtime.tasks import enqueue_task

    ws = f"pm-research-degrade-{uuid4().hex[:12]}"
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick", payload={"goal": "Ship it"})
    _backdate_workstream(conn, ws, days=30)  # would otherwise be due

    def boom(*a, **k):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(pm_mod, "_last_research_at", boom)

    r = run_pm_tick(conn, pm_task, NullEventSink(), call_model=_plan_call_model)
    # Core pm.tick still produced a plan; the scan was skipped (no crash, no scan).
    assert r.decision == "planned"
    assert r.research_task_id is None
    assert _count_research(conn, ws) == 0


def _total_tasks(conn, ws) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s", (ws,))
        n = cur.fetchone()["n"]
    conn.commit()
    return n
