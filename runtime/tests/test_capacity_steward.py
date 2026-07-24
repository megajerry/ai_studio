"""Capacity Steward (ADR-0022 C2) — the BEHAVIORAL layer on the deterministic engine.

Pure tests (no DB): the zone→action mapping, the config OFF-by-default gate, and the
body-free flag payloads. The prompt tests prove the additive budget-awareness section
is present when a role opts in and absent (byte-identical) when it does not. The DB
tests (SKIP cleanly with no Postgres) prove the steward FLAGS a workstream projected
to breach before its period ends + emits body-free ``capacity.*`` events, WITHOUT
enforcing or raising a ceiling; that the config gate activates it; and that a
``purpose`` threads through ``call_model`` end-to-end so a ``wind_down`` call is
permitted in the reserve zone while a ``normal`` one is blocked.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.enforce import MemoryEventSink
from runtime.event_types import EVENT_CAPACITY_FLAGGED, EVENT_CAPACITY_RECOMMENDATION
from runtime.roles.capacity_steward import (
    ACTION_COMPACT,
    ACTION_ESCALATE,
    ACTION_PIVOT,
    ACTION_REALLOCATE,
    CapacityFlag,
    capacity_steward_enabled,
    recommend_action,
    run_capacity_steward,
)
from runtime.roles.prompt import compose_role_prompt
from runtime.workstream import CapacityStewardSpec, WorkstreamConfig


# ===========================================================================
# 1. Pure logic — zone→action mapping + config gate + leak-free payloads
# ===========================================================================


def test_recommend_action_maps_every_zone():
    assert recommend_action("over", False) == ACTION_ESCALATE
    assert recommend_action("reserve", False) == ACTION_ESCALATE
    assert recommend_action("throttle", False) == ACTION_REALLOCATE
    assert recommend_action("warn", False) == ACTION_COMPACT
    # ok zone but projected to breach before period end → re-plan scope.
    assert recommend_action("ok", True) == ACTION_PIVOT


def test_capacity_steward_off_by_default_and_config_gated():
    # No config at all → PM stays the steward (off).
    assert capacity_steward_enabled(None) is False
    # Config without the block → off.
    assert capacity_steward_enabled(WorkstreamConfig(name="x")) is False
    # Explicitly disabled → off.
    off = WorkstreamConfig(name="x", capacity_steward=CapacityStewardSpec(enabled=False))
    assert capacity_steward_enabled(off) is False
    # Enabled via config → on (config-not-code).
    on = WorkstreamConfig(name="x", capacity_steward=CapacityStewardSpec(enabled=True))
    assert capacity_steward_enabled(on) is True


def test_capacity_steward_config_is_strict(tmp_path):
    """An unknown field under capacity_steward errors clearly (rules-as-data)."""
    from runtime.workstream import WorkstreamConfigError, load_config_file

    d = tmp_path / "bad"
    d.mkdir()
    (d / "config.yaml").write_text(
        "name: bad\ncapacity_steward:\n  enabled: true\n  bogus: 1\n", encoding="utf-8"
    )
    with pytest.raises(WorkstreamConfigError) as exc:
        load_config_file(d / "config.yaml")
    assert "bogus" in str(exc.value)


def test_flag_payloads_are_body_free_numbers_and_enums():
    flag = CapacityFlag(
        workstream="ws", period="monthly", zone="reserve", action=ACTION_ESCALATE,
        projected_breach=True, spent_usd=0.95, spent_tokens=950, cap_usd=1.0,
        cap_tokens=1000, remaining_usd=0.05, remaining_tokens=50,
        minutes_to_exhaustion=3.2, calls_to_exhaustion=2.0, horizon_minutes=9000.0,
    )
    flag_keys = set(flag.flag_payload())
    rec_keys = set(flag.recommendation_payload())
    allowed = {
        "workstream", "period", "zone", "action", "projected_breach",
        "spent_usd", "spent_tokens", "cap_usd", "cap_tokens", "remaining_usd",
        "remaining_tokens", "minutes_to_exhaustion", "calls_to_exhaustion",
        "horizon_minutes",
    }
    assert flag_keys.issubset(allowed) and rec_keys.issubset(allowed)
    # The recommendation carries the action enum from the closed vocabulary.
    assert flag.recommendation_payload()["action"] == ACTION_ESCALATE


# ===========================================================================
# 2. Budget-awareness prompt layer (additive, behavior-preserving)
# ===========================================================================


def test_budget_awareness_off_by_default_is_byte_identical():
    base = "You are the studio Executor. Goal: g."
    assert compose_role_prompt(base) == base
    assert compose_role_prompt(base, budget_aware=False) == base


def test_budget_awareness_section_present_when_opted_in():
    out = compose_role_prompt("BASE-PERSONA", budget_aware=True)
    assert "BASE-PERSONA" in out
    assert "### Budget awareness" in out
    # The concrete guidance every role now carries (ADR-0022 / ADR-0013).
    for token in ("warn", "throttle", "reserve", "COMPACT", "ESCALATE"):
        assert token in out


def test_roles_charter_carries_budget_awareness_guidance():
    """The core-loop roles' assembled prompts now contain the budget-awareness text
    (proven at the assembler with each role's persona)."""
    from runtime.roles.pm import _PLAN_PROMPT
    from runtime.roles.executor import _EXEC_PROMPT
    from runtime.roles.verifier import _VERIFY_PROMPT

    for base in (
        _PLAN_PROMPT.format(goal="g"),
        _EXEC_PROMPT.format(goal="g", criterion="c"),
        _VERIFY_PROMPT.format(criterion="c"),
    ):
        out = compose_role_prompt(base, budget_aware=True)
        assert "### Budget awareness" in out
        assert "compact" in out.lower() and "escalate" in out.lower()


# ===========================================================================
# 3. Live DB — steward flags/recommends; purpose threads through call_model
# ===========================================================================

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    from runtime.migrate import migrate

    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"cap-steward-{uuid4().hex[:12]}"


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover - only fires on a real network call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _seed_call(conn, ws, *, cost_usd=0.0, input_tokens=0, output_tokens=0, age_min=0):
    """Append a model.call event; optionally backdate its ts by ``age_min`` minutes
    (to place it outside the recent burn-rate look-back window)."""
    from runtime.events import append_event
    from runtime.models import make_event

    ev = append_event(
        conn,
        make_event(
            workstream=ws, type="model.call",
            payload={
                "model": "m", "provider": "dryrun", "role": "exec",
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cached_tokens": 0, "cost_usd": cost_usd, "latency_ms": 1,
            },
        ),
    )
    if age_min:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE events SET ts = now() - make_interval(mins => %s) WHERE id = %s",
                    (age_min, ev.id),
                )
    return ev


def _budget_approvals_for(conn, ws):
    from runtime.approvals import pending_approvals

    return [a for a in pending_approvals(conn) if a.tool == "model.call" and ws in a.reason]


def test_steward_flags_projected_breach_and_recommends_without_enforcing(conn, ws):
    from runtime import budget

    # cap 1000 tokens, graduated thresholds; seed 950 tokens spent → reserve zone,
    # and a burn that projects exhaustion well before the (monthly) period ends.
    budget.set_budget(
        conn, ws, cap_tokens=1000,
        warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
    )
    _seed_call(conn, ws, input_tokens=500, output_tokens=250)
    _seed_call(conn, ws, input_tokens=150, output_tokens=50)  # 950 total → 95% used

    sink = MemoryEventSink()
    report = run_capacity_steward(conn, sink, workstream=ws)

    # It flagged this workstream + recommended the reserve-zone action (escalate).
    assert report.flagged_count == 1
    flag = report.flags[0]
    assert flag.workstream == ws and flag.period == "monthly"
    assert flag.zone == "reserve"
    assert flag.action == ACTION_ESCALATE
    assert flag.projected_breach is True
    assert flag.minutes_to_exhaustion is not None
    assert flag.horizon_minutes is not None
    assert flag.minutes_to_exhaustion <= flag.horizon_minutes  # breach before period end

    # It emitted EXACTLY the two body-free capacity.* events — NOTHING else. In
    # particular NO budget.* enforcement event (it does not enforce) ...
    types = [e.type for e in sink.events]
    assert types == [EVENT_CAPACITY_FLAGGED, EVENT_CAPACITY_RECOMMENDATION]
    # ... and it raised NO 🛑 "raise budget" approval (that stays a PM/stakeholder call).
    assert _budget_approvals_for(conn, ws) == []

    # Events are leak-free (numbers/enums only) — no prompt/secret can appear.
    allowed = {
        "workstream", "period", "zone", "action", "projected_breach",
        "spent_usd", "spent_tokens", "cap_usd", "cap_tokens", "remaining_usd",
        "remaining_tokens", "minutes_to_exhaustion", "calls_to_exhaustion",
        "horizon_minutes",
    }
    for e in sink.events:
        assert set(e.payload).issubset(allowed), set(e.payload) - allowed


def test_steward_quiet_when_comfortably_under_cap(conn, ws):
    from runtime import budget

    # 40% of cap, no graduated thresholds, and the spend is OLD (outside the recent
    # burn window) → ok zone + zero recent burn → not projected to breach → quiet.
    budget.set_budget(conn, ws, cap_tokens=1000)
    _seed_call(conn, ws, input_tokens=300, output_tokens=100, age_min=180)  # 400/1000, 3h ago

    sink = MemoryEventSink()
    report = run_capacity_steward(conn, sink, workstream=ws)
    assert report.flagged_count == 0
    assert sink.events == []


def test_steward_off_by_default_config_gate_activates_it(conn, ws):
    """Config-not-code: with the steward disabled a gated run is a no-op; enabling it
    via the workstream config activates the same monitor."""
    from runtime import budget

    budget.set_budget(
        conn, ws, cap_tokens=1000,
        warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
    )
    _seed_call(conn, ws, input_tokens=700, output_tokens=250)  # 950 → reserve

    # OFF (default): PM is the steward → the gated run does not fire.
    cfg_off = WorkstreamConfig(name=ws)
    sink_off = MemoryEventSink()
    if capacity_steward_enabled(cfg_off):  # pragma: no cover - stays False
        run_capacity_steward(conn, sink_off, workstream=ws)
    assert capacity_steward_enabled(cfg_off) is False
    assert sink_off.events == []

    # ON (config opt-in): the same monitor now flags + recommends.
    cfg_on = WorkstreamConfig(
        name=ws, capacity_steward=CapacityStewardSpec(enabled=True)
    )
    sink_on = MemoryEventSink()
    assert capacity_steward_enabled(cfg_on) is True
    report = run_capacity_steward(conn, sink_on, workstream=ws)
    assert report.flagged_count == 1
    assert [e.type for e in sink_on.events] == [
        EVENT_CAPACITY_FLAGGED, EVENT_CAPACITY_RECOMMENDATION
    ]


def test_purpose_threads_through_call_model_reserve_zone(conn, ws):
    """End-to-end wiring: in the reserve zone a `normal` call is BLOCKED by the C1
    engine while a `wind_down` call is PERMITTED (the buffer funds winding down)."""
    from runtime import budget
    from runtime.enforce import DbEventSink
    from runtime.model.call import call_model

    # cap 1000 tokens, reserve at 0.90; seed 950 tokens → reserve zone.
    budget.set_budget(
        conn, ws, cap_tokens=1000,
        warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
    )
    _seed_call(conn, ws, input_tokens=700, output_tokens=250)

    short = [{"role": "user", "content": "hi"}]  # tiny est so we stay in reserve, not over

    # A NORMAL call is withheld (buffer preserved) — the C1 engine blocks it.
    calls_before = budget.spent(conn, ws).calls
    with pytest.raises(budget.OverBudget) as ei:
        call_model(
            "exec", "execute", short, workstream=ws, registry=None,
            sink=DbEventSink(conn), conn=conn, purpose="normal", force_dry_run=True,
        )
    assert ei.value.zone == "reserve"
    assert budget.spent(conn, ws).calls == calls_before  # nothing spent
    # No 🛑 approval — a reserve withhold is not a ceiling raise.
    assert _budget_approvals_for(conn, ws) == []

    # A WIND_DOWN call is PERMITTED through the same gate — it runs + accrues.
    comp = call_model(
        "exec", "execute", short, workstream=ws, registry=None,
        sink=DbEventSink(conn), conn=conn, purpose="wind_down", force_dry_run=True,
    )
    assert comp.provider == "dryrun"
    assert budget.spent(conn, ws).calls == calls_before + 1


def test_default_purpose_is_normal_behavior_preserving(conn, ws):
    """An existing caller that omits `purpose` behaves exactly as a `normal` one."""
    from runtime import budget
    from runtime.enforce import DbEventSink
    from runtime.model.call import call_model

    budget.set_budget(
        conn, ws, cap_tokens=1000,
        warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
    )
    _seed_call(conn, ws, input_tokens=700, output_tokens=250)  # reserve

    # No purpose kwarg → default normal → blocked in the reserve zone (unchanged).
    with pytest.raises(budget.OverBudget) as ei:
        call_model(
            "exec", "execute", [{"role": "user", "content": "hi"}],
            workstream=ws, registry=None, sink=DbEventSink(conn), conn=conn,
            force_dry_run=True,
        )
    assert ei.value.zone == "reserve"
