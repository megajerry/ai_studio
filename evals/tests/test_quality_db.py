"""Live-DB tests for the telemetry quality rollup (runtime.quality).

Seeds a fresh workstream with a KNOWN telemetry shape (merged/abandoned tasks,
verify pass/fail events, re-kicks, and model.call cost/token events) and asserts
:func:`runtime.quality.quality_report` computes the expected counts and rates.
SKIPs cleanly when no DATABASE_URL is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    pytest evals/tests/test_quality_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import TaskStatus, make_event
from runtime.quality import quality_report
from runtime.tasks import complete_task, enqueue_task, transition

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"eval-q-{uuid4().hex[:12]}"


def _drive_to_merged(conn, task_id):
    transition(conn, task_id, TaskStatus.CLAIMED, agent_id="w", claimed_by="w")
    transition(conn, task_id, TaskStatus.IN_PROGRESS, agent_id="w")
    complete_task(conn, task_id, status=TaskStatus.MERGED)


def _drive_to_abandoned(conn, task_id):
    transition(conn, task_id, TaskStatus.CLAIMED, agent_id="w", claimed_by="w")
    transition(conn, task_id, TaskStatus.IN_PROGRESS, agent_id="w")
    complete_task(conn, task_id, status=TaskStatus.ABANDONED, result={"why": "seed"})


def _model_call(conn, ws, task_id, *, cost, in_tok, out_tok):
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=task_id,
        payload={"model": "dryrun", "cost_usd": cost,
                 "input_tokens": in_tok, "output_tokens": out_tok, "latency_ms": 10},
    ))


def test_quality_report_computes_expected_metrics(conn, ws):
    # 2 merged tasks, 1 abandoned task.
    m1 = enqueue_task(conn, workstream=ws, type="work.a")
    m2 = enqueue_task(conn, workstream=ws, type="work.b")
    a1 = enqueue_task(conn, workstream=ws, type="work.c")
    _drive_to_merged(conn, m1.id)
    _drive_to_merged(conn, m2.id)
    _drive_to_abandoned(conn, a1.id)

    # 3 verify.passed + 1 verify.failed.
    for _ in range(3):
        append_event(conn, make_event(workstream=ws, type="verify.passed",
                                      task_id=m1.id, payload={"passed": True}))
    append_event(conn, make_event(workstream=ws, type="verify.failed",
                                  task_id=a1.id, payload={"passed": False}))

    # 2 re-kicks.
    for _ in range(2):
        append_event(conn, make_event(workstream=ws, type="task.rekicked",
                                      task_id=m1.id, payload={"retries": 1}))

    # model.call cost/tokens attributed to the two merged (completed) tasks.
    _model_call(conn, ws, m1.id, cost=0.01, in_tok=100, out_tok=50)   # 150 tokens
    _model_call(conn, ws, m2.id, cost=0.03, in_tok=200, out_tok=50)   # 250 tokens

    rep = quality_report(conn, workstream=ws)

    t = rep["totals"]
    assert t["tasks_merged"] == 2
    assert t["tasks_abandoned"] == 1
    assert t["tasks_terminal"] == 3
    assert t["verify_passed"] == 3
    assert t["verify_failed"] == 1
    assert t["rekicks"] == 2
    assert t["model_calls"] == 2
    assert abs(t["total_cost_usd"] - 0.04) < 1e-9
    assert t["total_tokens"] == 400

    r = rep["rates"]
    assert r["task_success_rate"] == round(2 / 3, 4)
    assert r["verify_pass_rate"] == round(3 / 4, 4)
    assert r["rekick_rate"] == round(2 / 3, 4)
    # error_rate = (abandoned + verify_failed) / (terminal + verify_passed + verify_failed)
    assert r["error_rate"] == round((1 + 1) / (3 + 3 + 1), 4)

    c = rep["cost"]
    assert c["completed_tasks"] == 2
    assert c["avg_cost_per_completed_task_usd"] == round(0.04 / 2, 4)
    assert c["avg_tokens_per_completed_task"] == round(400 / 2, 4)

    lat = rep["latency"]
    assert lat["avg_latency_per_completed_task_ms"] is not None
    assert lat["avg_latency_per_completed_task_ms"] >= 0


def test_quality_report_empty_workstream_has_none_rates(conn, ws):
    rep = quality_report(conn, workstream=ws)
    assert rep["totals"]["tasks_terminal"] == 0
    assert rep["rates"]["task_success_rate"] is None
    assert rep["rates"]["verify_pass_rate"] is None
    assert rep["cost"]["avg_cost_per_completed_task_usd"] is None


def test_quality_report_all_workstreams_aggregates(conn, ws):
    # Seed one merged task in this ws, then the global report must count >= it.
    m = enqueue_task(conn, workstream=ws, type="work.x")
    _drive_to_merged(conn, m.id)
    scoped = quality_report(conn, workstream=ws)["totals"]["tasks_merged"]
    allws = quality_report(conn, workstream=None)["totals"]["tasks_merged"]
    assert scoped == 1
    assert allws >= scoped
