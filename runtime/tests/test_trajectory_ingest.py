"""Tests for the ingestion bridge + rotation/TTL maintenance worker (ADR-0020, T5).

Two capabilities:

- :mod:`runtime.trajectory_ingest` — map an EXTERNAL trajectory JSON (e.g. an
  off-host session's orchestration log) onto the single GUARDED writer, and a CLI
  to feed a file. Proven against a real Postgres (SKIP cleanly off-host).
- :mod:`runtime.trajectory_worker` — a periodic sweep that (a) enforces the TTL via
  ``expire_trajectories`` and (b) rotates OLD, already-MINED verbatim trajectories
  to ``lean``. Live-DB tests prove the expiry + rotation behavior; DB-free tests
  prove the ADR-0017 graceful degradation (loop survives an outage; one failing
  rotation never sinks the pass).

Run::

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_trajectory_ingest.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from runtime import db
from runtime import trajectory_ingest as ti
from runtime import trajectory_worker as tw
from runtime.db import DBUnavailable
from runtime.event_types import (
    EVENT_RETRO_COMPLETED,
    EVENT_TRAJECTORY_CLOSED,
    EVENT_TRAJECTORY_STARTED,
    EVENT_TRAJECTORY_STEP_ADDED,
)
from runtime.events import append_event, make_event, read_events
from runtime.migrate import migrate
from runtime.trajectory import (
    RETENTION_LEAN,
    RETENTION_VERBATIM,
    add_step,
    close_trajectory,
    expire_trajectories,
    get_trajectory,
    list_steps,
    start_trajectory,
)

# Live-DB tests skip cleanly when no Postgres is reachable; the DB-free degradation
# tests below are NOT gated so they always run.
needs_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0011 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-ingest-{uuid4().hex[:10]}"


# --- ingestion bridge -------------------------------------------------------


@needs_db
def test_ingest_maps_external_record_via_guarded_writer(conn, ws):
    record = {
        "role": "executor",
        "workstream": ws,
        "goal": "ingest an off-host episode",
        "context_size_start": 500,
        "outcome_summary": "done well",
        "steps": [
            {"step_type": "observe", "summary": "looked", "rationale": "r0"},
            {
                "step_type": "plan", "summary": "planned", "rationale": "r1",
                "options_considered": ["p", "q"], "choice": "x", "confidence": 0.5,
                "refs": {"k": "v"}, "context_size": 700, "tokens": 12, "latency_ms": 9,
            },
            {"step_type": "commit", "summary": "committed", "rationale": "r2"},
        ],
    }
    tid = ti.ingest_trajectory(conn, record)

    traj = get_trajectory(conn, tid)
    assert traj is not None
    assert traj.role == "executor" and traj.workstream == ws
    assert traj.goal == "ingest an off-host episode"      # body persisted verbatim
    assert traj.retention_tier == RETENTION_VERBATIM
    assert traj.status == "closed"                        # close default True
    assert traj.context_size_start == 500
    assert traj.outcome_summary == "done well"

    steps = list_steps(conn, tid)
    assert [s.seq for s in steps] == [1, 2, 3]            # order preserved, gapless
    assert [s.summary for s in steps] == ["looked", "planned", "committed"]
    assert [s.step_type for s in steps] == ["observe", "plan", "commit"]
    assert steps[0].rationale == "r0"
    assert steps[1].choice == "x"
    assert abs(steps[1].confidence - 0.5) < 1e-6
    assert steps[1].refs == {"k": "v"}
    assert steps[1].options_considered == ["p", "q"]
    assert steps[1].context_size == 700 and steps[1].tokens == 12

    # The write path emitted the body-free trajectory.* events (guarded writer).
    traj_evs = {e.type for e in read_events(conn, workstream=ws)
                if e.type.startswith("trajectory.")}
    assert {EVENT_TRAJECTORY_STARTED, EVENT_TRAJECTORY_STEP_ADDED,
            EVENT_TRAJECTORY_CLOSED} <= traj_evs


@needs_db
def test_ingest_honors_timestamps_and_ttl(conn, ws):
    t0 = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone.utc)
    record = {
        "role": "pm", "workstream": ws, "goal": "timed episode",
        "ttl": 3600, "started_at": t0.isoformat(),
        "ended_at": (t0 + timedelta(seconds=30)).isoformat(),
        "steps": [{"step_type": "plan", "summary": "s",
                   "created_at": (t0 + timedelta(seconds=5)).isoformat()}],
    }
    tid = ti.ingest_trajectory(conn, record)
    traj = get_trajectory(conn, tid)
    assert traj.started_at == t0
    assert traj.expires_at == t0 + timedelta(seconds=3600)  # started + ttl
    assert traj.latency_ms == 30000                          # ended - started


@needs_db
def test_cli_ingests_temp_file(conn, ws, tmp_path, capsys):
    record = {"role": "pm", "workstream": ws, "goal": "cli episode",
              "steps": [{"step_type": "observe", "summary": "cli-step"}]}
    path = tmp_path / "traj.json"
    path.write_text(json.dumps(record))

    rc = ti.main([str(path)])
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    tid = UUID(printed)                                   # CLI prints the new id

    traj = get_trajectory(conn, tid)
    assert traj is not None and traj.workstream == ws
    assert [s.summary for s in list_steps(conn, tid)] == ["cli-step"]


# --- TTL sweep --------------------------------------------------------------


@needs_db
def test_ttl_sweep_removes_exactly_expired(conn, ws):
    base = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    # expire is a GLOBAL sweep; clear anything already expired at base first so the
    # count is deterministic across re-runs (mirrors test_trajectory_db).
    expire_trajectories(conn, now=base)
    expired = start_trajectory(conn, "pm", ws, "old", ttl=1, now=base - timedelta(hours=2))
    live = start_trajectory(conn, "pm", ws, "fresh", ttl=3600, now=base)
    forever = start_trajectory(conn, "pm", ws, "eternal", now=base)

    result = tw.sweep_once(conn, rotate_after_s=3600, rotate_enabled=False, now=base)
    assert result.expired == 1                            # exactly the expired row
    assert result.rotated == []                           # rotation disabled
    assert get_trajectory(conn, expired) is None
    assert get_trajectory(conn, live) is not None
    assert get_trajectory(conn, forever) is not None


# --- verbatim -> lean rotation ----------------------------------------------


def _seed_closed_verbatim(conn, ws, goal, *, ended_at, choice, rationale):
    tid = start_trajectory(conn, "pm", ws, goal, now=ended_at - timedelta(hours=1))
    add_step(conn, tid, "decide", "chose it", rationale=rationale,
             options_considered=["A", "B"], choice=choice, confidence=0.77,
             refs={"critic_verdict": "proceed"}, now=ended_at - timedelta(hours=1))
    close_trajectory(conn, tid, outcome_summary=f"{choice} worked out", now=ended_at)
    return tid


def _mark_mined(conn, ws, tid):
    """Record the "mined" marker the worker keys on: a retro.completed referencing
    the trajectory (exactly what the wired Retro role emits)."""
    append_event(conn, make_event(
        workstream=ws, type=EVENT_RETRO_COMPLETED,
        payload={"trajectory_id": str(tid), "lessons_count": 1}))


@needs_db
def test_rotation_flips_old_mined_verbatim_to_lean(conn, ws):
    base = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    long_rationale = "First line is the gist.\n" + "verbose detail line\n" * 30

    # (1) OLD + MINED verbatim → should rotate.
    mined = _seed_closed_verbatim(conn, ws, "old mined",
                                  ended_at=base - timedelta(days=10),
                                  choice="B", rationale=long_rationale)
    _mark_mined(conn, ws, mined)

    # (2) OLD but UN-mined (no retro.completed) → must stay verbatim.
    unmined = _seed_closed_verbatim(conn, ws, "old unmined",
                                    ended_at=base - timedelta(days=10),
                                    choice="C", rationale=long_rationale)

    # (3) FRESH + mined (recent, not old enough) → must stay verbatim.
    fresh = _seed_closed_verbatim(conn, ws, "fresh mined", ended_at=base,
                                  choice="D", rationale=long_rationale)
    _mark_mined(conn, ws, fresh)

    result = tw.sweep_once(conn, rotate_after_s=7 * 24 * 3600,
                           rotate_enabled=True, now=base)

    assert mined in result.rotated
    assert unmined not in result.rotated
    assert fresh not in result.rotated

    # The mined one is now lean, with outcome-relevant fields PRESERVED.
    rotated_traj = get_trajectory(conn, mined)
    assert rotated_traj.retention_tier == RETENTION_LEAN
    assert rotated_traj.outcome_summary == "B worked out"
    s = list_steps(conn, mined)[0]
    assert s.rationale == "First line is the gist."       # distilled to first line
    assert s.choice == "B"
    assert abs(s.confidence - 0.77) < 1e-6
    assert s.refs == {"critic_verdict": "proceed"}
    assert s.options_considered == ["A", "B"]

    # The un-mined + fresh ones are untouched (still verbatim, full rationale).
    assert get_trajectory(conn, unmined).retention_tier == RETENTION_VERBATIM
    assert get_trajectory(conn, fresh).retention_tier == RETENTION_VERBATIM
    assert list_steps(conn, unmined)[0].rationale == long_rationale


@needs_db
def test_rotation_is_idempotent(conn, ws):
    base = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    tid = _seed_closed_verbatim(conn, ws, "rotate twice",
                                ended_at=base - timedelta(days=10),
                                choice="B", rationale="gist line.\nmore\nmore")
    _mark_mined(conn, ws, tid)

    first = tw.rotate_mined_trajectories(conn, older_than_s=7 * 24 * 3600, now=base)
    assert tid in first
    # Second pass: already lean → verbatim-only selection skips it (no re-rotate).
    second = tw.rotate_mined_trajectories(conn, older_than_s=7 * 24 * 3600, now=base)
    assert tid not in second


@needs_db
def test_retro_marks_mined_via_trajectory_link(conn, ws):
    """End-to-end wiring: a retro on a task LINKED to a trajectory emits a
    retro.completed carrying trajectory_id, which the worker then selects."""
    from runtime.enforce import DbEventSink
    from runtime.roles.retro import run_retro
    from runtime.tasks import enqueue_task

    base = datetime(2026, 7, 23, 0, 0, 0, tzinfo=timezone.utc)
    tid = _seed_closed_verbatim(conn, ws, "decompose episode",
                                ended_at=base - timedelta(days=10),
                                choice="B", rationale="gist.\nx\ny")
    # Link a work task to the trajectory (as a PM decompose step would).
    target = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    with conn.cursor() as cur:
        cur.execute("UPDATE tasks SET trajectory_id = %s WHERE id = %s", (tid, target.id))
    conn.commit()
    # A minimal trail for the target so run_retro has something to read.
    append_event(conn, make_event(workstream=ws, type="task.finished", task_id=target.id))

    retro_task = enqueue_task(conn, workstream=ws, type="retro",
                              payload={"target_task_id": str(target.id),
                                       "target_task_type": "work.demo", "outcome": "done"})
    run_retro(conn, retro_task, DbEventSink(conn))

    # The retro marked the episode mined → the worker now rotates it.
    rotated = tw.rotate_mined_trajectories(conn, older_than_s=7 * 24 * 3600, now=base)
    assert tid in rotated
    assert get_trajectory(conn, tid).retention_tier == RETENTION_LEAN


# --- graceful degradation (ADR-0017) — no live DB required ------------------


def test_run_loop_degrades_on_db_unavailable():
    """The maintenance loop survives a DB outage: it logs degraded + retries each
    interval (never crashes / hangs), exactly like the supervisor."""
    calls = {"n": 0}

    def fake_connect(**kw):
        calls["n"] += 1
        raise DBUnavailable("store unreachable", attempts=3)

    tw.run(connect=fake_connect, sleep=lambda _s: None,
           max_iterations=3, rotate_enabled=False)
    assert calls["n"] == 3                                # retried each iteration


def test_run_loop_swallows_sweep_error(monkeypatch):
    """A non-connectivity bug in a sweep must not kill the maintenance process."""
    class FakeConn:
        closed = False
        def close(self):  # noqa: D401
            pass

    def boom(*a, **k):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(tw, "sweep_once", boom)
    # Must not raise.
    tw.run(connect=lambda **kw: FakeConn(), sleep=lambda _s: None, max_iterations=2)


def test_rotation_degrades_per_item(monkeypatch):
    """One failing compaction is skipped, not fatal — the rest still rotate."""
    ids = [uuid4(), uuid4(), uuid4()]
    monkeypatch.setattr(tw, "select_rotatable", lambda conn, **kw: list(ids))

    def compact(conn, tid, distill_fn, *, now=None):
        if tid == ids[1]:
            raise RuntimeError("write failed")
        return object()

    rotated = tw.rotate_mined_trajectories(None, older_than_s=1, compact=compact)
    assert rotated == [ids[0], ids[2]]                    # the failing one skipped
