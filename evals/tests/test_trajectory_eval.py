"""Trajectory decision-quality eval tests.

Pure tests cover the id-free item projection and the honest dry-run/real pass gate
(no DB). The live-DB test seeds a real PM trajectory into the
``trajectories``/``trajectory_steps`` tables and scores it via the swappable judge.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from evals.judge import Judge, JudgeVerdict
from evals.trajectory_eval import (
    TrajectoryEvalResult,
    build_trajectory_item,
    cleanup_trajectory_shape,
    run_trajectory_eval,
    seed_demo_trajectory,
)
from runtime import db


def _fake_verdict(*, dry_run: bool, passed: bool) -> JudgeVerdict:
    return JudgeVerdict(
        rubric_id="pm_decision_quality", passed=passed, score=0.5,
        rationale="x", provider="dryrun" if dry_run else "anthropic",
        dry_run=dry_run, raw_text="",
    )


def test_build_item_is_id_free():
    traj = SimpleNamespace(goal="do it", outcome_summary="done")
    steps = [
        SimpleNamespace(step_type="decide", summary="chose B",
                        options_considered=["A", "B"], choice="B", confidence=0.8),
    ]
    item = build_trajectory_item(traj, steps)
    assert item["goal"] == "do it" and item["num_steps"] == 1
    assert item["steps"][0]["choice"] == "B"
    # No UUIDs / timestamps leak into the item (keeps the dryrun score stable).
    blob = str(item)
    assert "uuid" not in blob.lower() and "created_at" not in blob


def test_passed_gate_dryrun_is_mechanism_only():
    # On a dryrun judge, passed reflects the MECHANISM (verdict produced), not the
    # dryrun pass/fail — even a dryrun "fail" counts as mechanism-passed.
    r = TrajectoryEvalResult("tid", "pm_decision_quality",
                             _fake_verdict(dry_run=True, passed=False))
    assert r.passed is True


def test_passed_gate_real_model_honors_verdict():
    ok = TrajectoryEvalResult("tid", "pm_decision_quality",
                              _fake_verdict(dry_run=False, passed=True))
    bad = TrajectoryEvalResult("tid", "pm_decision_quality",
                               _fake_verdict(dry_run=False, passed=False))
    assert ok.passed is True and bad.passed is False


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_seeded_trajectory_scored_by_dryrun_judge(monkeypatch):
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    conn = db.connect()
    # Own throwaway workstream so the seeded episode is cleaned up by prefix (a
    # passed-in trajectory_id is NOT auto-cleaned by run_trajectory_eval).
    ws = f"eval-traj-{uuid4().hex[:8]}"
    try:
        from runtime.migrate import migrate
        migrate(conn)

        # Seed a real, well-formed PM trajectory into the tables.
        tid = seed_demo_trajectory(conn, workstream=ws)

        result = run_trajectory_eval(conn, tid, judge=Judge(force_dry_run=True))
        assert result.trajectory_id == str(tid)
        assert result.rubric_id == "pm_decision_quality"
        assert result.verdict.dry_run is True
        assert result.verdict.provider == "dryrun"
        assert result.passed is True  # mechanism ran

        # Deterministic: judging the same persisted trajectory again matches.
        again = run_trajectory_eval(conn, tid, judge=Judge(force_dry_run=True))
        assert again.verdict.score == result.verdict.score

        d = result.to_dict()
        assert d["name"] == "pm_trajectory_decision_quality"
        assert d["verdict"]["provider"] == "dryrun"
        assert d["item"]["goal"]  # the goal was projected into the item

        # Throwaway teardown: no rows of this seeder's own prefix survive the test.
        cleanup_trajectory_shape(conn, ws)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM trajectories WHERE workstream = %s",
                        (ws,))
            assert cur.fetchone()["n"] == 0
        conn.commit()
    finally:
        cleanup_trajectory_shape(conn, ws)
        conn.close()


@pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)
def test_run_trajectory_eval_autoseeds_when_no_id(monkeypatch):
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    conn = db.connect()
    try:
        from runtime.migrate import migrate
        migrate(conn)
        result = run_trajectory_eval(conn, judge=Judge(force_dry_run=True))
        assert result.trajectory_id  # auto-seeded a trajectory to score
        assert result.verdict.dry_run is True
    finally:
        conn.close()
