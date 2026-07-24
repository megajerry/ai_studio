"""Live-DB tests for the failure telemetry rollup (ADR-0023 R3).

Seed a KNOWN failure shape (``model.call.failed`` by ``error_type`` + ``task.stuck``
by ``stall_reason`` + terminal transitions + verify signals), then assert the exact
counts, the rates with ``n`` + Wilson 95% CI + small-sample flag, the per-category
shares, the ``since_seq`` post-fix window, and None-safe/empty behavior. SKIP cleanly
when no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_quality_failure_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import make_event
from runtime.quality import failure_report, quality_report, wilson_interval

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
    return f"fail-{uuid4().hex[:12]}"


# --- seeding helpers (KNOWN failure shapes) ---------------------------------


def _model_ok(conn, ws, n):
    for _ in range(n):
        append_event(conn, make_event(workstream=ws, type="model.call",
                                       payload={"model": "dryrun", "cost_usd": 0.0,
                                                "input_tokens": 1, "output_tokens": 1}))


def _model_failed(conn, ws, error_type, n):
    for _ in range(n):
        append_event(conn, make_event(
            workstream=ws, type="model.call.failed",
            payload={"error_type": error_type, "model": "m", "provider": "p",
                     "role": "executor", "task_type": "work"}))


def _stuck(conn, ws, stall_reason, n):
    for _ in range(n):
        append_event(conn, make_event(workstream=ws, type="task.stuck",
                                       payload={"stall_reason": stall_reason,
                                                "no_progress_rekicks": 3, "retries": 3}))


def _transition(conn, ws, to, n):
    for _ in range(n):
        append_event(conn, make_event(workstream=ws, type="task.transition",
                                       payload={"to": to}))


def _rekick(conn, ws, n):
    for _ in range(n):
        append_event(conn, make_event(workstream=ws, type="task.rekicked",
                                       payload={"made_progress": False}))


# --- known-shape counts + rates + CI ----------------------------------------


def test_failure_report_known_shape(conn, ws):
    _model_ok(conn, ws, 60)
    _model_failed(conn, ws, "RateLimitError", 40)   # 40 of 100 calls
    _model_failed(conn, ws, "TimeoutError", 5)      # +5 → 105 calls, 45 failed
    _stuck(conn, ws, "no_progress", 4)
    _rekick(conn, ws, 6)
    _transition(conn, ws, "merged", 10)
    _transition(conn, ws, "abandoned", 5)           # terminal = 15
    conn.commit()

    rep = failure_report(conn, ws)
    t = rep["totals"]
    assert t["model_calls_ok"] == 60
    assert t["model_calls_failed"] == 45
    assert t["model_calls_total"] == 105
    assert t["rekicks"] == 6 and t["stucks"] == 4
    assert t["tasks_terminal"] == 15

    # Headline model-call error rate: 45/105, carrying n + Wilson CI + flag.
    er = rep["rates"]["model_call_error_rate"]
    assert er["successes"] == 45 and er["n"] == 105
    assert er["rate"] == round(45 / 105, 4)
    assert er["ci95"] == wilson_interval(45, 105)
    assert er["insufficient_sample"] is False  # n=105 >= 30

    # rekick_rate over terminal (6/15).
    rk = rep["rates"]["rekick_rate"]
    assert rk["successes"] == 6 and rk["n"] == 15 and rk["rate"] == 0.4

    # by_error_type: sorted desc, each a share of ALL calls (n=105).
    by = {e["error_type"]: e for e in rep["by_error_type"]}
    assert rep["by_error_type"][0]["error_type"] == "RateLimitError"  # largest first
    assert by["RateLimitError"]["count"] == 40
    assert by["RateLimitError"]["share"]["n"] == 105
    assert by["RateLimitError"]["share"]["rate"] == round(40 / 105, 4)
    assert by["RateLimitError"]["share"]["ci95"] == wilson_interval(40, 105)
    assert by["TimeoutError"]["count"] == 5

    # by_stall_reason: share of terminal tasks (n=15).
    sr = {e["stall_reason"]: e for e in rep["by_stall_reason"]}
    assert sr["no_progress"]["count"] == 4
    assert sr["no_progress"]["share"]["n"] == 15
    assert sr["no_progress"]["share"]["rate"] == round(4 / 15, 4)


# --- None-safe empty --------------------------------------------------------


def test_failure_report_empty_is_none_safe(conn):
    empty = f"fail-empty-{uuid4().hex[:8]}"
    rep = failure_report(conn, empty)
    assert rep["totals"]["model_calls_total"] == 0
    assert rep["by_error_type"] == [] and rep["by_stall_reason"] == []
    for r in rep["rates"].values():
        assert r["rate"] is None and r["ci95"] is None and r["n"] == 0
        assert r["insufficient_sample"] is True


# --- since_seq post-fix window ----------------------------------------------


def test_failure_report_since_seq_scopes_to_post_fix_window(conn, ws):
    # Pre-fix: a high failure rate.
    _model_ok(conn, ws, 10)
    _model_failed(conn, ws, "RateLimitError", 40)
    conn.commit()

    # Cursor captured "when the fix is applied".
    with conn.cursor() as cur:
        cur.execute("SELECT max(seq) AS s FROM events")
        cursor = int(cur.fetchone()["s"])
    conn.commit()

    # Post-fix: mostly healthy traffic.
    _model_ok(conn, ws, 90)
    _model_failed(conn, ws, "RateLimitError", 10)
    conn.commit()

    whole = failure_report(conn, ws)
    post = failure_report(conn, ws, since_seq=cursor)

    # Whole history sees all 50 failures / 150 calls.
    assert whole["totals"]["model_calls_failed"] == 50
    assert whole["totals"]["model_calls_total"] == 150
    # Post-fix window sees only what happened AFTER the cursor: 10/100.
    assert post["totals"]["model_calls_failed"] == 10
    assert post["totals"]["model_calls_total"] == 100
    assert post["rates"]["model_call_error_rate"]["rate"] == 0.1


# --- integration into quality_report (additive, no regression) --------------


def test_quality_report_includes_failure_section(conn, ws):
    _model_ok(conn, ws, 30)
    _model_failed(conn, ws, "RateLimitError", 20)
    conn.commit()

    rep = quality_report(conn, ws)
    # Existing sections preserved.
    assert {"totals", "rates", "cost", "latency", "by_model_global",
            "pm_decision_quality", "grounding_global", "capacity_global"} <= set(rep)
    # New failure section present + correct.
    assert "failure" in rep
    f = rep["failure"]
    assert f["totals"]["model_calls_failed"] == 20
    assert f["by_error_type"][0]["error_type"] == "RateLimitError"
