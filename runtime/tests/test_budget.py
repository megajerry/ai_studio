"""Per-workstream budget enforcement — pure logic + live-DB gating (ADR-0006/0012).

Pure tests need no database. The DB tests SKIP cleanly (never error, never hang)
when no Postgres is reachable — the same probe the other integration suites use.
They prove: caps read from real accrued `model.call` cost; `spent`/`remaining`
correctness; period windowing; over-cap → the next model call is gated
(`OverBudget` raised, no `model.call` emitted, a 🛑 approval row created); under-cap
→ the call proceeds + accrues; budget events leak nothing; migration idempotent.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.enforce import DbEventSink, MemoryEventSink
from runtime.policy import BudgetContext


# --- Pure logic (no DB) -----------------------------------------------------


def test_budget_context_usd_and_token_caps_would_exceed():
    # Token cap alone.
    assert BudgetContext(spent_tokens=90, budget_tokens=100, estimated_tokens=20).would_exceed
    assert not BudgetContext(spent_tokens=90, budget_tokens=100, estimated_tokens=5).would_exceed
    # USD cap alone.
    assert BudgetContext(spent_usd=0.9, budget_usd=1.0, estimated_usd=0.2).would_exceed
    assert not BudgetContext(spent_usd=0.9, budget_usd=1.0, estimated_usd=0.05).would_exceed
    # Either cap breaking is enough; both None = never.
    assert BudgetContext(spent_usd=2.0, budget_usd=1.0, spent_tokens=1, budget_tokens=100).would_exceed
    assert not BudgetContext(spent_tokens=10, spent_usd=10.0).would_exceed


def test_exceed_reason_is_leak_free_numbers_only():
    ctx = BudgetContext(
        spent_tokens=100, budget_tokens=100, estimated_tokens=10,
        spent_usd=1.0, budget_usd=1.0, estimated_usd=0.1,
    )
    reason = ctx.exceed_reason()
    assert "tokens" in reason and "usd" in reason
    # No prompt/secret text can appear — it's built only from numbers.
    assert "user" not in reason and "content" not in reason


def test_invalid_period_rejected():
    from runtime.budget import VALID_PERIODS, _window_clause

    with pytest.raises(ValueError):
        _window_clause("weekly")
    # Each valid period yields a boolean fragment over ts (or TRUE for all_time).
    for p in VALID_PERIODS:
        assert isinstance(_window_clause(p), str)
    assert _window_clause("all_time") == "TRUE"


def test_estimate_call_tokens_matches_dry_run_basis():
    from runtime.budget import estimate_call_tokens

    msgs = [{"role": "user", "content": "x" * 400}]
    # 400 chars / 4 = 100 input; output = 100 // 4 = 25 → 125 total.
    assert estimate_call_tokens(msgs) == 125
    assert estimate_call_tokens([]) >= 1  # never zero


def test_budget_fingerprint_stable_and_period_scoped():
    from runtime.budget import _budget_fingerprint

    a = _budget_fingerprint("ws", "monthly")
    assert a == _budget_fingerprint("ws", "monthly")  # stable
    assert a != _budget_fingerprint("ws", "daily")  # period-scoped
    assert a != _budget_fingerprint("other", "monthly")  # workstream-scoped


def test_over_budget_exception_message_is_leak_free():
    from runtime.budget import BudgetStatus, OverBudget

    st = BudgetStatus(
        workstream="ws", period="monthly", cap_usd=1.0, cap_tokens=None,
        spent_usd=1.5, spent_tokens=0, est_usd=0.0, est_tokens=0,
    )
    exc = OverBudget(st)
    assert exc.workstream == "ws" and exc.period == "monthly"
    assert "ws" in str(exc) and "usd" in str(exc)


# --- Live DB ----------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    from runtime.migrate import migrate

    c = db.connect()
    migrate(c)  # ensure schema (incl. 0010_budgets) exists
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"budget-test-{uuid4().hex[:12]}"


def _seed_call(conn, ws, *, cost_usd, input_tokens=0, output_tokens=0, age_days=0):
    """Append a model.call event; optionally backdate its ts by `age_days`."""
    from runtime.events import append_event
    from runtime.models import make_event

    ev = append_event(
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
    if age_days:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE events SET ts = now() - make_interval(days => %s) WHERE id = %s",
                    (age_days, ev.id),
                )
    return ev


def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "MODELS_DRY_RUN"):
        monkeypatch.delenv(env, raising=False)


def _budget_approvals_for(conn, ws):
    """Pending 🛑 raise-budget approvals for a workstream (the reason names it)."""
    from runtime.approvals import pending_approvals

    return [
        a for a in pending_approvals(conn)
        if a.tool == "model.call" and ws in a.reason
    ]


# -- config CRUD --


def test_set_get_list_budget(conn, ws):
    from runtime import budget

    assert budget.get_budget(conn, ws) is None
    b = budget.set_budget(conn, ws, cap_usd=10.0, cap_tokens=1000)
    assert b.cap_usd == 10.0 and b.cap_tokens == 1000 and b.period == "monthly"
    # Upsert updates in place (still one row).
    budget.set_budget(conn, ws, cap_usd=20.0)
    got = budget.get_budget(conn, ws)
    assert got.cap_usd == 20.0 and got.cap_tokens is None
    budget.set_budget(conn, ws, period="daily", cap_usd=1.0)
    assert {b.period for b in budget.list_budgets(conn, ws)} == {"monthly", "daily"}


# -- spent / remaining from real model.call cost --


def test_spent_and_remaining_from_real_events(conn, ws):
    from runtime import budget

    _seed_call(conn, ws, cost_usd=1.25, input_tokens=100, output_tokens=20)
    _seed_call(conn, ws, cost_usd=0.75, input_tokens=50, output_tokens=10)
    s = budget.spent(conn, ws)
    assert s.calls == 2
    assert s.cost_usd == pytest.approx(2.0)
    assert s.tokens == 180  # (100+20)+(50+10)

    budget.set_budget(conn, ws, cap_usd=5.0, cap_tokens=1000)
    st = budget.remaining(conn, ws)
    assert st.remaining_usd == pytest.approx(3.0)
    assert st.remaining_tokens == 820
    # No cap set → remaining() is None.
    assert budget.remaining(conn, f"none-{uuid4().hex[:6]}") is None


def test_period_windowing(conn, ws):
    from runtime import budget

    _seed_call(conn, ws, cost_usd=1.0, input_tokens=10)  # today
    _seed_call(conn, ws, cost_usd=4.0, input_tokens=40, age_days=2)   # 2d ago
    _seed_call(conn, ws, cost_usd=8.0, input_tokens=80, age_days=40)  # 40d ago

    # Daily excludes anything older than today.
    assert budget.spent(conn, ws, period="daily").cost_usd == pytest.approx(1.0)
    # Rolling 30d includes today + 2d ago, excludes 40d ago.
    assert budget.spent(conn, ws, period="rolling_30d").cost_usd == pytest.approx(5.0)
    # All-time includes everything.
    assert budget.spent(conn, ws, period="all_time").cost_usd == pytest.approx(13.0)


# -- enforcement: over cap gates, under cap accrues --


def test_over_cap_gates_next_model_call(conn, ws, monkeypatch):
    from runtime import budget
    from runtime.approvals import STATUS_PENDING
    from runtime.events import read_events
    from runtime.model.call import call_model

    _keyless(monkeypatch)

    # Seed spend already well over a tiny USD cap.
    _seed_call(conn, ws, cost_usd=5.0, input_tokens=1000, output_tokens=200)
    budget.set_budget(conn, ws, cap_usd=1.0)

    calls_before = budget.spent(conn, ws).calls
    sink = DbEventSink(conn)
    with pytest.raises(budget.OverBudget):
        call_model(
            "exec", "execute",
            [{"role": "user", "content": "do the thing " * 20}],
            workstream=ws, registry=None, sink=sink, conn=conn,
        )
    # The blocked call emitted NO new model.call (never spent) — the call count is
    # unchanged from the single seeded event.
    assert budget.spent(conn, ws).calls == calls_before
    types = [e.type for e in read_events(conn, workstream=ws)]
    assert "budget.exceeded" in types
    assert "model.routed" in types  # routing is logged before the block
    # A single 🛑 "raise budget" approval is pending for this workstream.
    pend = _budget_approvals_for(conn, ws)
    assert len(pend) == 1
    assert pend[0].tier == "🛑" and pend[0].status == STATUS_PENDING
    assert "raise budget" in pend[0].reason.lower()

    # A SECOND blocked call reuses the same approval (idempotent, no pile-up).
    with pytest.raises(budget.OverBudget):
        call_model(
            "exec", "execute", [{"role": "user", "content": "again"}],
            workstream=ws, registry=None, sink=sink, conn=conn,
        )
    assert len(_budget_approvals_for(conn, ws)) == 1


def test_under_cap_proceeds_and_accrues(conn, ws, monkeypatch):
    from runtime import budget
    from runtime.events import read_events
    from runtime.model.call import call_model

    _keyless(monkeypatch)

    budget.set_budget(conn, ws, cap_usd=1000.0, cap_tokens=10_000_000)
    before = budget.spent(conn, ws)
    comp = call_model(
        "exec", "execute",
        [{"role": "user", "content": "do the thing " * 30}],
        workstream=ws, registry=None, sink=DbEventSink(conn), conn=conn,
    )
    assert comp.provider == "dryrun"
    after = budget.spent(conn, ws)
    # The call proceeded (a model.call was emitted) and cost accrued.
    assert after.calls == before.calls + 1
    assert after.tokens == before.tokens + comp.usage.total_tokens
    types = [e.type for e in read_events(conn, workstream=ws)]
    assert "budget.checkpoint" in types and "model.call" in types


def test_no_budget_configured_is_noop(conn, ws, monkeypatch):
    from runtime.events import read_events
    from runtime.model.call import call_model

    _keyless(monkeypatch)
    # No set_budget → enforcement is a no-op; the call runs, no budget events.
    call_model(
        "exec", "execute", [{"role": "user", "content": "hello"}],
        workstream=ws, registry=None, sink=DbEventSink(conn), conn=conn,
    )
    types = [e.type for e in read_events(conn, workstream=ws)]
    assert "model.call" in types
    assert "budget.checkpoint" not in types and "budget.exceeded" not in types


def test_budget_events_leak_nothing(conn, ws):
    from runtime import budget

    budget.set_budget(conn, ws, cap_usd=100.0)
    sink = MemoryEventSink()
    secret = "SUPER-SECRET-PROMPT-TEXT"
    budget.enforce(conn, ws, est_usd=0.01, est_tokens=10, role="exec", sink=sink)
    assert [e.type for e in sink.events] == ["budget.checkpoint"]
    blob = str([e.payload for e in sink.events])
    assert secret not in blob
    # Payload keys are the known leak-free set only.
    allowed = {
        "workstream", "period", "cap_usd", "cap_tokens", "spent_usd",
        "spent_tokens", "est_usd", "est_tokens", "remaining_usd",
        "remaining_tokens", "reason",
    }
    for e in sink.events:
        assert set(e.payload).issubset(allowed)


def test_token_cap_enforced_independently(conn, ws):
    from runtime import budget

    # Token cap breached even though there is no USD cap.
    _seed_call(conn, ws, cost_usd=0.0, input_tokens=900, output_tokens=200)
    budget.set_budget(conn, ws, cap_tokens=1000)
    sink = MemoryEventSink()
    with pytest.raises(budget.OverBudget):
        budget.enforce(
            conn, ws, est_usd=0.0, est_tokens=1, role="exec", sink=sink,
            request_approval=lambda *a, **k: None,  # approval loop covered elsewhere
        )
    assert [e.type for e in sink.events] == ["budget.exceeded"]


def test_migration_idempotent(conn):
    from runtime.migrate import migrate

    # Re-running applies nothing new and the budgets table is intact.
    assert "0010_budgets.sql" not in migrate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM budgets")
        assert cur.fetchone()["n"] >= 0
    conn.commit()
