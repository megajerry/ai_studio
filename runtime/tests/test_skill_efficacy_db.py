"""Live-DB tests for ``skill_efficacy_report`` (ADR-0024 P1).

Seed KNOWN applied vs baseline cohorts of comparable tasks and assert the report
recovers the exploration means (iterations / input tokens / tool+search calls) and
outcome rates (first-pass-merge / verify-pass), each with its sample size + Wilson
CI (rates) or mean (counts) + ``insufficient_sample`` flag; that pooling across
similar task_types reaches ``n``; that a perfect-but-tiny cohort (1.0 on n=3) is
flagged, never trusted; and that no data is None-safe. Exercises the metric against
REAL human-authored skill names (``define-success-criteria`` / ``rigorous-review``).
SKIP cleanly when no DATABASE_URL is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_skill_efficacy_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.event_types import EVENT_SKILL_APPLIED
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import TaskStatus as S
from runtime.models import make_event
from runtime.quality import MIN_TRUSTWORTHY_SAMPLE, quality_report, skill_efficacy_report
from runtime.tasks import enqueue_task, transition
from runtime.trajectory import add_step, start_trajectory

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
    return f"skilleff-{uuid4().hex[:12]}"


def _seed_task(conn, ws, ttype, skill, *, steps, in_tokens, tools, verify="pass"):
    """One first-pass-merged task in family ``ttype`` with KNOWN efficiency metrics.

    ``skill`` (or ``None``) sets whether a body-free ``skill.applied`` attributes it.
    ``steps`` trajectory steps (iterations), ``in_tokens`` model.call input tokens,
    ``tools`` tool+search calls, and a passing/failing verify gate.
    """
    tid = start_trajectory(conn, "executor", ws, f"do {ttype}")
    for i in range(steps):
        add_step(conn, tid, "plan", f"step {i}")
    t = enqueue_task(conn, workstream=ws, type=ttype, payload={}, trajectory_id=tid)
    # First-pass merge (no reviewer_blocked / rework round-trip).
    for st in (S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED):
        assert transition(conn, t.id, st) is not None
    if skill is not None:
        append_event(conn, make_event(
            workstream=ws, type=EVENT_SKILL_APPLIED, task_id=t.id,
            payload={"skills": [skill], "role": "executor"}))
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=t.id,
        payload={"input_tokens": in_tokens, "output_tokens": 0, "cost_usd": 0}))
    for _ in range(tools):
        append_event(conn, make_event(
            workstream=ws, type="tool.invoked", task_id=t.id, payload={}))
    append_event(conn, make_event(
        workstream=ws, type=f"verify.{'passed' if verify == 'pass' else 'failed'}",
        task_id=t.id, payload={}))
    conn.commit()
    return t


def _skill_entry(rep, name):
    return next((s for s in rep["by_skill"] if s["skill"] == name), None)


# --- pooling reaches n + metric recovery ------------------------------------


def test_pooling_reaches_n_and_recovers_applied_vs_baseline(conn, ws):
    # Applied cohort: LOW exploration, pooled across two task_types of one family.
    for _ in range(18):
        _seed_task(conn, ws, "work.a", "define-success-criteria",
                   steps=2, in_tokens=100, tools=1)
    for _ in range(14):
        _seed_task(conn, ws, "work.b", "define-success-criteria",
                   steps=2, in_tokens=100, tools=1)
    # Baseline cohort: HIGHER exploration, same family, NO skill.
    for _ in range(16):
        _seed_task(conn, ws, "work.a", None, steps=6, in_tokens=500, tools=4)
    for _ in range(16):
        _seed_task(conn, ws, "work.b", None, steps=6, in_tokens=500, tools=4)

    rep = skill_efficacy_report(conn, ws)
    assert rep["skills_measured"] == 1
    ent = _skill_entry(rep, "define-success-criteria")
    assert ent is not None and ent["applied_task_count"] == 32

    # Similar task_types pooled into ONE family "work" (design: reach n).
    assert len(ent["by_task_family"]) == 1
    fam = ent["by_task_family"][0]
    assert fam["task_family"] == "work"
    assert fam["task_types_pooled"] == ["work.a", "work.b"]

    a, b = fam["applied"], fam["baseline"]
    assert a["n_tasks"] == 32 and b["n_tasks"] == 32

    # Exploration means recovered; pooled n>=30 → NOT flagged insufficient.
    assert a["iterations"]["mean"] == 2.0 and a["iterations"]["n"] == 32
    assert a["iterations"]["insufficient_sample"] is False
    assert b["iterations"]["mean"] == 6.0
    assert a["input_tokens"]["mean"] == 100.0 and b["input_tokens"]["mean"] == 500.0
    assert a["tool_search_calls"]["mean"] == 1.0 and b["tool_search_calls"]["mean"] == 4.0

    # Delta = applied - baseline: NEGATIVE = the applied cohort explored LESS.
    assert fam["delta"]["iterations_mean"] == -4.0
    assert fam["delta"]["input_tokens_mean"] == -400.0
    assert fam["delta"]["tool_search_calls_mean"] == -3.0

    # Outcome rates carry n + Wilson CI + flag (both cohorts first-pass + verified).
    fp = a["first_pass_merge_rate"]
    assert fp["rate"] == 1.0 and fp["n"] == 32 and fp["insufficient_sample"] is False
    assert fp["ci95"] is not None and fp["ci95"][0] < 1.0  # honest: perfect != certain
    assert a["verify_pass_rate"]["rate"] == 1.0 and a["verify_pass_rate"]["n"] == 32


# --- tiny sample is flagged, a 1.0 is never trusted -------------------------


def test_tiny_sample_flagged_and_perfect_rate_not_trustworthy(conn, ws):
    for _ in range(3):  # n=3 applied, all perfect → must be flagged, not trusted
        _seed_task(conn, ws, "review", "rigorous-review",
                   steps=1, in_tokens=50, tools=1)
    _seed_task(conn, ws, "review", None, steps=4, in_tokens=200, tools=3)

    rep = skill_efficacy_report(conn, ws)
    ent = _skill_entry(rep, "rigorous-review")
    fam = ent["by_task_family"][0]
    a = fam["applied"]
    assert a["n_tasks"] == 3
    fp = a["first_pass_merge_rate"]
    assert fp["rate"] == 1.0 and fp["n"] == 3
    assert fp["insufficient_sample"] is True          # n<30 → not trustworthy
    assert fp["ci95"][0] < 1.0                          # Wilson lower bound honest
    assert a["iterations"]["insufficient_sample"] is True
    assert MIN_TRUSTWORTHY_SAMPLE == 30


# --- None-safe: no applied skills -------------------------------------------


def test_none_safe_on_no_data(conn, ws):
    rep = skill_efficacy_report(conn, ws)
    assert rep["skills_measured"] == 0 and rep["by_skill"] == []
    assert rep["pooling"]["min_trustworthy_sample"] == MIN_TRUSTWORTHY_SAMPLE


# --- wired into quality_report additively -----------------------------------


def test_quality_report_carries_skill_efficacy_plus_all_prior_sections(conn, ws):
    _seed_task(conn, ws, "work.a", "define-success-criteria",
               steps=2, in_tokens=100, tools=1)
    rep = quality_report(conn, ws)
    # New section present...
    assert "skill_efficacy" in rep
    assert rep["skill_efficacy"]["skills_measured"] >= 1
    # ...and every prior section preserved (additive, no regressions).
    for key in ("totals", "rates", "cost", "latency", "by_model_global",
                "pm_decision_quality", "grounding_global", "capacity_global",
                "failure"):
        assert key in rep, f"quality_report lost prior section {key!r}"
