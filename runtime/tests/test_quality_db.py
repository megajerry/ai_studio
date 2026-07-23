"""Live-DB tests for outcome-attribution + CI-aware PM decision-quality (T3).

Seed trajectories + the tasks they created (``tasks.trajectory_id``) + KNOWN
lifecycle transition shapes, then assert the exact outcome counts, rates, Wilson
95% bounds, the small-sample flag, and None-safe/empty behavior. SKIP cleanly when
no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_quality_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.migrate import migrate
from runtime.models import TaskStatus as S
from runtime.quality import pm_decision_quality, quality_report
from runtime.tasks import enqueue_task, transition
from runtime.trajectory import start_trajectory

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0011 (trajectories + tasks.trajectory_id) applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"qual-{uuid4().hex[:12]}"


# --- seeding helpers (KNOWN outcome shapes) ---------------------------------


def _linked_task(conn, ws, tid):
    """A ``work.*`` task attributed to trajectory ``tid`` (the decomposition link)."""
    t = enqueue_task(conn, workstream=ws, type="work.x", payload={})
    with conn.cursor() as cur:
        cur.execute("UPDATE tasks SET trajectory_id = %s WHERE id = %s", (tid, t.id))
    conn.commit()
    return t


def _drive(conn, task_id, *statuses):
    for s in statuses:
        assert transition(conn, task_id, s) is not None


def _first_pass_merge(conn, ws, tid):
    """merged with NO reviewer_blocked / rework — a clean first pass."""
    t = _linked_task(conn, ws, tid)
    _drive(conn, t.id, S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    return t


def _rework_then_merge(conn, ws, tid):
    """merged, but only after a reviewer_blocked → in_progress round-trip (rework)."""
    t = _linked_task(conn, ws, tid)
    _drive(conn, t.id, S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW,
           S.REVIEWER_BLOCKED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    return t


def _escalated_then_abandoned(conn, ws, tid):
    """parked 'blocked' on a 🔴 approval (escalation), then abandoned."""
    t = _linked_task(conn, ws, tid)
    _drive(conn, t.id, S.CLAIMED, S.IN_PROGRESS, S.BLOCKED, S.ABANDONED)
    return t


def _abandoned(conn, ws, tid):
    """abandoned outright (no escalation, no rework)."""
    t = _linked_task(conn, ws, tid)
    _drive(conn, t.id, S.CLAIMED, S.IN_PROGRESS, S.ABANDONED)
    return t


def _in_progress(conn, ws, tid):
    """still in flight — no terminal outcome yet (excluded from n_terminal)."""
    t = _linked_task(conn, ws, tid)
    _drive(conn, t.id, S.CLAIMED, S.IN_PROGRESS)
    return t


# --- first-pass + small-sample flag + Wilson bounds -------------------------


def test_all_first_pass_flags_small_sample_with_ci(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decompose the roadmap")
    for _ in range(5):
        _first_pass_merge(conn, ws, tid)

    rep = pm_decision_quality(conn, ws)
    assert rep["trajectories_scored"] == 1
    tr = rep["by_trajectory"][0]
    assert tr["role"] == "pm" and tr["workstream"] == ws
    assert tr["n_tasks"] == 5 and tr["n_terminal"] == 5

    fp = tr["metrics"]["first_pass_merge_rate"]
    assert fp["successes"] == 5 and fp["n"] == 5 and fp["rate"] == 1.0
    # The defect fix: a perfect-but-tiny sample is FLAGGED and its CI is wide, not
    # a bare "1.0". Hand-recomputed Wilson for 5/5 → [0.566, 1.0].
    assert fp["insufficient_sample"] is True
    lo, hi = fp["ci95"]
    assert round(lo, 3) == 0.566 and hi == 1.0

    # The other axes are all zero on this clean trajectory.
    m = tr["metrics"]
    assert m["rework_rate"]["successes"] == 0
    assert m["escalation_rate"]["successes"] == 0
    assert m["abandoned_rate"]["successes"] == 0


# --- exact mixed-outcome counts + rates -------------------------------------


def test_mixed_outcomes_exact_counts_and_rates(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decompose feature B")
    for _ in range(2):
        _first_pass_merge(conn, ws, tid)     # 2 clean merges
    _rework_then_merge(conn, ws, tid)        # 1 merged after rework
    _escalated_then_abandoned(conn, ws, tid) # 1 escalated + abandoned
    _abandoned(conn, ws, tid)                # 1 plain abandoned
    _in_progress(conn, ws, tid)              # 1 still in flight (not terminal)

    tr = pm_decision_quality(conn, ws)["by_trajectory"][0]
    assert tr["n_tasks"] == 6            # all six linked tasks
    assert tr["n_terminal"] == 5         # the in-flight one is excluded from n

    m = tr["metrics"]
    assert m["first_pass_merge_rate"]["successes"] == 2
    assert m["first_pass_merge_rate"]["rate"] == 0.4
    assert m["rework_rate"]["successes"] == 1 and m["rework_rate"]["rate"] == 0.2
    assert m["escalation_rate"]["successes"] == 1 and m["escalation_rate"]["rate"] == 0.2
    assert m["abandoned_rate"]["successes"] == 2 and m["abandoned_rate"]["rate"] == 0.4
    # Every rate shares the terminal denominator and is flagged (n=5 < 30).
    assert all(m[k]["n"] == 5 for k in m)
    assert all(m[k]["insufficient_sample"] is True for k in m)

    # Hand-recomputed Wilson for the rework rate 1/5 → [0.0362, 0.6245].
    assert m["rework_rate"]["ci95"] == (0.0362, 0.6245)


# --- aggregation (per role + overall) + trustworthy-sample threshold --------


def test_aggregate_pooling_and_sample_threshold(conn, ws):
    t_small = start_trajectory(conn, "pm", ws, "small decision")
    for _ in range(5):
        _first_pass_merge(conn, ws, t_small)
    t_big = start_trajectory(conn, "pm", ws, "big decision")
    for _ in range(30):
        _first_pass_merge(conn, ws, t_big)

    rep = pm_decision_quality(conn, ws)
    assert rep["trajectories_scored"] == 2

    # Per-trajectory: n=30 crosses the trustworthy threshold; n=5 does not.
    per = {t["n_terminal"]: t for t in rep["by_trajectory"]}
    assert per[30]["metrics"]["first_pass_merge_rate"]["insufficient_sample"] is False
    assert per[5]["metrics"]["first_pass_merge_rate"]["insufficient_sample"] is True

    # Pooled by role: 35 terminal tasks → trustworthy sample.
    pm = rep["by_role"]["pm"]
    assert pm["trajectories"] == 2 and pm["n_terminal"] == 35
    fp = pm["metrics"]["first_pass_merge_rate"]
    assert fp["successes"] == 35 and fp["rate"] == 1.0
    assert fp["insufficient_sample"] is False

    # Overall mirrors the single-role pool here.
    ov = rep["overall"]
    assert ov["n_terminal"] == 35
    assert ov["metrics"]["first_pass_merge_rate"]["insufficient_sample"] is False


# --- None-safe / empty workstream -------------------------------------------


def test_empty_workstream_is_none_safe(conn):
    empty = f"qual-empty-{uuid4().hex[:8]}"
    rep = pm_decision_quality(conn, empty)
    assert rep["trajectories_scored"] == 0
    assert rep["by_trajectory"] == [] and rep["by_role"] == {}
    ov = rep["overall"]
    assert ov["n_tasks"] == 0 and ov["n_terminal"] == 0
    for metric in ov["metrics"].values():
        assert metric["rate"] is None and metric["ci95"] is None
        assert metric["n"] == 0 and metric["successes"] == 0
        assert metric["insufficient_sample"] is True


# --- integration into quality_report (no regression) ------------------------


def test_quality_report_includes_pm_section(conn, ws):
    tid = start_trajectory(conn, "pm", ws, "decompose")
    _first_pass_merge(conn, ws, tid)
    _abandoned(conn, ws, tid)

    rep = quality_report(conn, ws)
    # Existing sections are preserved.
    assert {"totals", "rates", "cost", "latency", "by_model_global"} <= set(rep)
    # New outcome-attribution section is present + correct.
    assert "pm_decision_quality" in rep
    pmq = rep["pm_decision_quality"]
    assert pmq["workstream"] == ws
    tr = pmq["by_trajectory"][0]
    assert tr["n_terminal"] == 2
    assert tr["metrics"]["first_pass_merge_rate"]["successes"] == 1
    assert tr["metrics"]["abandoned_rate"]["successes"] == 1


def test_quality_report_existing_rates_stay_none_guarded_on_empty(conn):
    empty = f"qual-empty-{uuid4().hex[:8]}"
    rep = quality_report(conn, empty)
    # Existing behavior unchanged: no divide-by-zero, rates are None on empty.
    assert rep["rates"]["task_success_rate"] is None
    assert rep["rates"]["verify_pass_rate"] is None
    assert rep["rates"]["error_rate"] is None
    # And the new section is None-safe too.
    assert rep["pm_decision_quality"]["overall"]["n_terminal"] == 0
