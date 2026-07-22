"""Live-DB end-to-end tests for the EXPERIMENT primitive (ADR-0016).

Exercise the full lifecycle against a real Postgres and SKIP cleanly when none is
reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_experiment_db.py

Covered: propose → start (tagged work enqueued) → evaluate for kept / killed /
scaled; over-budget → killed (from real ``model.call`` spend telemetry); scale →
🛑 approval row created; illegal status transition rejected; ``experiment.*``
events carry no hypothesis / secret text; migration idempotent. Keyless/dry-run.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.approvals import STATUS_PENDING, get_approval
from runtime.enforce import DbEventSink
from runtime.events import append_event, read_events
from runtime.experiment import (
    EVENT_EVALUATED,
    EVENT_PROPOSED,
    EVENT_STARTED,
    ExperimentDecision,
    ExperimentStatus,
    IllegalTransition,
    SuccessMetric,
    evaluate_experiment,
    get_experiment,
    list_experiments,
    propose_experiment,
    record_observation,
    start_experiment,
)
from runtime.migrate import migrate
from runtime.models import make_event

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0009 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-exp-{uuid4().hex[:10]}"


# --- migration idempotency --------------------------------------------------


def test_migration_is_idempotent(conn):
    # Runner skips applied files; re-running must be a clean no-op and the table
    # + composite index must exist.
    migrate(conn)
    migrate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.experiments') AS t")
        assert cur.fetchone()["t"] == "experiments"
        cur.execute("SELECT to_regclass('public.experiments_workstream_status_idx') AS i")
        assert cur.fetchone()["i"] == "experiments_workstream_status_idx"
    conn.commit()


# --- propose / start --------------------------------------------------------


def _metric(name="signal", target=100.0, comparator=">=", aggregate="last") -> SuccessMetric:
    return SuccessMetric(name=name, target=target, comparator=comparator, aggregate=aggregate)


def test_propose_creates_proposed_row_and_event(conn, ws):
    sink = DbEventSink(conn)
    exp = propose_experiment(
        conn, workstream=ws, hypothesis="a bounded bet", metric=_metric(),
        budget_tokens=1000, budget_usd=1.0, sink=sink,
    )
    assert exp.status is ExperimentStatus.PROPOSED and exp.decision is None
    assert get_experiment(conn, exp.id).status is ExperimentStatus.PROPOSED
    assert exp in [] or list_experiments(conn, workstream=ws, status=ExperimentStatus.PROPOSED)
    events = [e for e in read_events(conn, workstream=ws) if e.type == EVENT_PROPOSED]
    assert len(events) == 1
    assert events[0].payload["experiment_id"] == str(exp.id)
    assert events[0].payload["metric_name"] == "signal"


def test_start_enqueues_tagged_work_and_runs(conn, ws):
    sink = DbEventSink(conn)
    exp = propose_experiment(
        conn, workstream=ws, hypothesis="h", metric=_metric(), budget_tokens=1000, sink=sink,
    )
    exp = start_experiment(
        conn, exp.id, sink=sink,
        work_items=[{"type": "work.probe", "payload": {"goal": "g"}, "budget_tokens": 500}],
    )
    assert exp.status is ExperimentStatus.RUNNING and exp.started_at is not None
    # Work task exists and is tagged with the experiment id.
    with conn.cursor() as cur:
        cur.execute("SELECT id, payload FROM tasks WHERE payload->>'experiment_id' = %s", (str(exp.id),))
        rows = cur.fetchall()
    conn.commit()
    assert len(rows) == 1 and rows[0]["payload"]["experiment_id"] == str(exp.id)
    started = [e for e in read_events(conn, workstream=ws) if e.type == EVENT_STARTED]
    assert started and len(started[0].payload["task_ids"]) == 1


# --- evaluate: kept / killed / scaled ---------------------------------------


def _running(conn, ws, sink, **metric_kw):
    exp = propose_experiment(
        conn, workstream=ws, hypothesis="h", metric=_metric(**metric_kw),
        budget_tokens=1_000_000, budget_usd=1000.0, sink=sink,
    )
    return start_experiment(conn, exp.id, sink=sink)


def test_evaluate_kept_when_metric_met(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)  # target 100, >=
    record_observation(conn, exp.id, 110.0, sink=sink, workstream=ws)  # met, not strong
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.KEPT and exp.decision is ExperimentDecision.KEPT
    assert exp.observed_value == 110.0 and exp.evaluated_at is not None


def test_evaluate_killed_when_metric_missed(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)
    record_observation(conn, exp.id, 40.0, sink=sink, workstream=ws)  # missed target 100
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.KILLED and exp.decision is ExperimentDecision.KILLED


def test_evaluate_killed_when_no_evidence(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)  # no observations recorded
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.KILLED  # a bet with no signal is killed


def test_evaluate_scaled_raises_red_approval(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)  # target 100, >=
    record_observation(conn, exp.id, 200.0, sink=sink, workstream=ws)  # strongly met
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.SCALED and exp.decision is ExperimentDecision.SCALED
    # A 🛑 approval for added budget was opened and linked via the evaluated event.
    ev = next(e for e in read_events(conn, workstream=ws) if e.type == EVENT_EVALUATED)
    approval_id = ev.payload["scale_approval_id"]
    assert approval_id is not None
    a = get_approval(conn, approval_id)
    assert a is not None and a.status == STATUS_PENDING
    assert a.tier == "red" and a.tool == "experiment.scale"


def test_evaluate_over_budget_kills_even_when_metric_strong(conn, ws):
    sink = DbEventSink(conn)
    # Tight token budget; a strong metric must NOT save an over-budget bet.
    exp = propose_experiment(
        conn, workstream=ws, hypothesis="h", metric=_metric(), budget_tokens=100, sink=sink,
    )
    exp = start_experiment(
        conn, exp.id, sink=sink, work_items=[{"type": "work.probe"}],
    )
    # Real spend telemetry: a model.call on the tagged task blows the 100-token budget.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tasks WHERE payload->>'experiment_id' = %s", (str(exp.id),))
        task_id = cur.fetchone()["id"]
    conn.commit()
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=task_id,
        payload={"model": "dryrun", "input_tokens": 400, "output_tokens": 100, "cost_usd": 0.0},
    ))
    record_observation(conn, exp.id, 500.0, sink=sink, workstream=ws)  # strong metric...
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.KILLED  # ...but over budget → killed
    assert exp.spent_tokens == 500 and exp.spent_tokens > 100


def test_evaluate_cost_metric_reads_spend_telemetry(conn, ws):
    """A cost metric needs NO observation — the value is read from task_cost."""
    sink = DbEventSink(conn)
    exp = propose_experiment(
        conn, workstream=ws, hypothesis="cheap signal",
        metric=_metric(name="total_tokens", target=1000.0, comparator="<="),
        budget_tokens=1_000_000, sink=sink,
    )
    exp = start_experiment(conn, exp.id, sink=sink, work_items=[{"type": "work.probe"}])
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM tasks WHERE payload->>'experiment_id' = %s", (str(exp.id),))
        task_id = cur.fetchone()["id"]
    conn.commit()
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=task_id,
        payload={"model": "dryrun", "input_tokens": 600, "output_tokens": 300, "cost_usd": 0.0},
    ))
    # 900 tokens <= 1000 → met, but 900 > 1000/1.25 (=800) → not "strong" → kept.
    exp = evaluate_experiment(conn, exp.id, sink=sink)
    assert exp.status is ExperimentStatus.KEPT and exp.observed_value == 900.0


# --- guards + secret hygiene ------------------------------------------------


def test_illegal_transition_evaluate_before_start(conn, ws):
    sink = DbEventSink(conn)
    exp = propose_experiment(conn, workstream=ws, hypothesis="h", metric=_metric(), sink=sink)
    with pytest.raises(IllegalTransition):  # proposed → evaluated is not legal
        evaluate_experiment(conn, exp.id, sink=sink)


def test_illegal_transition_double_evaluate(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)
    record_observation(conn, exp.id, 110.0, sink=sink, workstream=ws)
    evaluate_experiment(conn, exp.id, sink=sink)  # → kept (terminal)
    with pytest.raises(IllegalTransition):  # terminal → evaluated rejected
        evaluate_experiment(conn, exp.id, sink=sink)


def test_start_illegal_from_terminal(conn, ws):
    sink = DbEventSink(conn)
    exp = _running(conn, ws, sink)
    record_observation(conn, exp.id, 40.0, sink=sink, workstream=ws)
    evaluate_experiment(conn, exp.id, sink=sink)  # → killed
    with pytest.raises(IllegalTransition):
        start_experiment(conn, exp.id, sink=sink)


def test_events_carry_no_secret_hypothesis_text(conn, ws):
    sink = DbEventSink(conn)
    secret = f"SECRET_SAUCE_{uuid4().hex}"
    exp = propose_experiment(
        conn, workstream=ws, hypothesis=f"do not leak {secret}", metric=_metric(),
        budget_tokens=1_000_000, budget_usd=1000.0, sink=sink,
    )
    exp = start_experiment(conn, exp.id, sink=sink)
    record_observation(conn, exp.id, 200.0, sink=sink, workstream=ws)
    evaluate_experiment(conn, exp.id, sink=sink)  # scaled → also opens an approval

    for ev in read_events(conn, workstream=ws):
        blob = str(ev.payload)
        assert secret not in blob, f"secret leaked in {ev.type}: {blob}"
        assert "hypothesis" not in ev.payload  # the free-text field never travels
