"""PM + Critic reasoning-trajectory instrumentation tests (ADR-0020, T2).

Prove the PM persists its reasoning as a first-class trajectory and that the link
back to the work it decomposes is stamped, without changing any PM/Critic decision:

- a PM run produces ONE ``pm`` trajectory whose steps appear in the expected causal
  order with the expected ``step_type``s and non-empty VERBATIM rationale;
- decompose stamps ``tasks.trajectory_id`` on EVERY created task (queried directly);
- the bounded PM↔Critic loop records ``consult`` (verdict in choice/refs, concerns
  in rationale) + ``revise`` steps;
- DB-outage/degradation: a trajectory-write failure NEVER blocks/crashes the PM's
  core function (it still plans + decomposes) — ADR-0017.

Live-DB; SKIP cleanly when no Postgres is reachable (off-host). Keyless/dry-run.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.enforce import DbEventSink
from runtime.migrate import migrate
from runtime.roles.critic import run_critic
from runtime.roles.pm import run_pm_tick
from runtime.tasks import enqueue_task
from runtime.trajectory import list_steps

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.delenv("PM_CRITIC_ROUNDS", raising=False)

    def boom(*a, **k):  # pragma: no cover - only on a real network call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


def _plan_completion(plan: dict):
    return type("C", (), {"text": json.dumps(plan)})()


def _good_plan() -> dict:
    """A feasible, confident, fully-specified plan (no gaps → no critic block)."""
    return {
        "restated_goal": "Ship the thing",
        "confidence": 0.9,
        "feasible": True,
        "success_criteria": ["the thing is shipped and verified"],
        "work_items": [
            {"title": "P1", "type": "work.task", "instructions": "do p1",
             "success_criterion": "p1 done", "marker": "m1"},
            {"title": "P2", "type": "work.task", "instructions": "do p2",
             "success_criterion": "p2 done", "marker": "m2"},
        ],
    }


def _gap_plan() -> dict:
    """A feasible, confident plan with fixable gaps (no criteria/markers)."""
    return {
        "restated_goal": "Build X", "confidence": 0.9, "feasible": True,
        "success_criteria": [],
        "work_items": [
            {"title": "P1", "type": "work.task", "instructions": "do p1"},
            {"title": "P2", "type": "work.task", "instructions": "do p2"},
        ],
    }


def _infeasible_plan() -> dict:
    return {"restated_goal": "impossible", "confidence": 0.1, "feasible": False,
            "reason": "out of scope for the studio", "work_items": []}


def _pm_trajectory_id(conn, ws):
    """The single ``pm`` trajectory opened for this (unique) workstream."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM trajectories WHERE workstream = %s AND role = 'pm'", (ws,)
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    assert len(rows) == 1, f"expected exactly one pm trajectory, got {len(rows)}"
    return rows[0]["id"]


# --- 1. A PM plan run produces an ordered, verbatim trajectory ---------------


def test_pm_plan_produces_ordered_verbatim_trajectory(conn):
    ws = f"traj-pm-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Ship the thing"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_good_plan()),
    )
    assert result.decision == "planned" and result.work_item_count == 2

    tid = _pm_trajectory_id(conn, ws)
    steps = list_steps(conn, tid)
    # Expected causal order for the planned path (no critic wired).
    assert [s.step_type for s in steps] == [
        "observe", "plan", "decide", "decompose", "commit"
    ]
    # Every step carries a non-empty VERBATIM rationale (the whole point).
    for s in steps:
        assert s.rationale and s.rationale.strip(), f"empty rationale on {s.step_type}"
    # The decide step is the confidence gate: carries the self-score + choice.
    decide = next(s for s in steps if s.step_type == "decide")
    assert decide.choice == "proceed"
    assert abs(decide.confidence - 0.9) < 1e-6
    # The decompose step refs the exact created task ids.
    decompose = next(s for s in steps if s.step_type == "decompose")
    assert set(decompose.refs["task_ids"]) == set(result.work_task_ids)

    # The trajectory was closed with an outcome summary.
    from runtime.trajectory import get_trajectory
    traj = get_trajectory(conn, tid)
    assert traj.status == "closed" and traj.outcome_summary


# --- 2. Decompose stamps tasks.trajectory_id on every created task -----------


def test_decompose_stamps_trajectory_id_on_every_task(conn):
    ws = f"traj-link-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Ship the thing"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_good_plan()),
    )
    assert result.decision == "planned"
    tid = _pm_trajectory_id(conn, ws)

    # Query the tasks table directly: every created work task links to the trajectory.
    with conn.cursor() as cur:
        cur.execute("SELECT id, trajectory_id FROM tasks WHERE workstream = %s "
                    "AND type LIKE 'work.%%'", (ws,))
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    assert len(rows) == 2
    assert all(str(r["trajectory_id"]) == str(tid) for r in rows), rows
    # The attribution join is now possible.
    linked = {str(r["id"]) for r in rows}
    assert linked == set(result.work_task_ids)


# --- 3. The Critic loop records consult + revise steps with the verdict ------


def test_critic_loop_records_consult_and_revise_steps(conn):
    ws = f"traj-critic-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Build X"})
    # The gap plan blocks in round 1 (real critic) → PM revises → proceeds round 2.
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_gap_plan()),
        critic=run_critic, critic_rounds=2,
    )
    assert result.decision == "planned" and result.work_item_count == 2

    tid = _pm_trajectory_id(conn, ws)
    steps = list_steps(conn, tid)
    types = [s.step_type for s in steps]
    # Bounded loop recorded: 2 consults (round1 blocking, round2 proceed) + 1 revise.
    assert types == [
        "observe", "plan", "decide",
        "consult", "revise", "consult",
        "decompose", "commit",
    ], types

    consults = [s for s in steps if s.step_type == "consult"]
    # The Critic's verdict rides on choice + refs; concerns are the verbatim body.
    r1 = consults[0]
    assert r1.choice == "revise"                       # round 1 blocked → revise
    assert r1.refs["blocking"] is True and r1.refs["concern_count"] >= 1
    assert r1.rationale and "criterion" in r1.rationale  # verbatim concern body
    r2 = consults[1]
    assert r2.choice == "proceed" and r2.refs["blocking"] is False

    revise = next(s for s in steps if s.step_type == "revise")
    assert revise.choice == "revise" and revise.rationale.strip()


# --- 4. DB-outage / degradation: a trajectory-write failure never breaks PM --


def test_trajectory_write_failure_does_not_break_pm(conn, monkeypatch):
    """Simulate a trajectory-write outage: the PM must still plan + decompose."""
    ws = f"traj-degrade-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)

    def boom(*a, **k):
        raise RuntimeError("simulated trajectory-write outage")

    # Fail EVERY step append (the guarded writer path the PM records through).
    monkeypatch.setattr("runtime.roles.pm.add_step", boom)

    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Ship the thing"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_good_plan()),
    )
    # Core function preserved: the PM still decided + decomposed, no crash.
    assert result.decision == "planned" and result.work_item_count == 2
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s "
                    "AND type LIKE 'work.%%'", (ws,))
        assert int(cur.fetchone()["n"]) == 2
    if not conn.autocommit:
        conn.commit()


def test_trajectory_start_failure_degrades_to_no_trajectory(conn, monkeypatch):
    """If even opening the trajectory fails, the PM plans exactly as before."""
    ws = f"traj-nostart-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)

    def boom(*a, **k):
        raise RuntimeError("simulated DB outage on start_trajectory")

    monkeypatch.setattr("runtime.roles.pm.start_trajectory", boom)

    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "Ship the thing"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_good_plan()),
    )
    assert result.decision == "planned" and result.work_item_count == 2
    # No trajectory opened → the created tasks simply carry a NULL link (not blocked).
    with conn.cursor() as cur:
        cur.execute("SELECT trajectory_id FROM tasks WHERE workstream = %s "
                    "AND type LIKE 'work.%%'", (ws,))
        rows = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM trajectories WHERE workstream = %s", (ws,))
        n_traj = int(cur.fetchone()["n"])
    if not conn.autocommit:
        conn.commit()
    assert len(rows) == 2 and all(r["trajectory_id"] is None for r in rows)
    assert n_traj == 0


# --- pushback / clarify paths also record + close a trajectory ---------------


def test_infeasible_plan_records_decide_escalate_and_closes(conn):
    ws = f"traj-pushback-{uuid4().hex[:12]}"
    sink = DbEventSink(conn)
    pm_task = enqueue_task(conn, workstream=ws, type="pm.tick",
                           payload={"goal": "do the impossible"})
    result = run_pm_tick(
        conn, pm_task, sink,
        call_model=lambda **kw: _plan_completion(_infeasible_plan()),
        request_approval=lambda *a, **k: type("A", (), {"id": uuid4()})(),
    )
    assert result.decision == "pushback"
    tid = _pm_trajectory_id(conn, ws)
    steps = list_steps(conn, tid)
    assert [s.step_type for s in steps] == ["observe", "plan", "decide", "escalate"]
    assert next(s for s in steps if s.step_type == "decide").choice == "pushback"
    from runtime.trajectory import get_trajectory
    assert get_trajectory(conn, tid).status == "closed"
