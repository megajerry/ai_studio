"""Live-DB tests for the graduated recovery ladder (ADR-0023, R1).

Exercises the progress-aware recovery mechanics end-to-end against a real Postgres:
the nudge + grace rung, the progress detector, the early stuck escalation
(``task.stuck``), and the unchanged max_retries abandon backstop. SKIP cleanly when
no DATABASE_URL is reachable (off-host sandbox).

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_recovery_ladder_db.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from runtime import db
from runtime.event_types import EVENT_MODEL_CALL
from runtime.events import append_event, read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus, make_event
from runtime.supervisor import sweep
from runtime.tasks import (
    claim_task,
    enqueue_task,
    escalate_stuck_task,
    heartbeat,
    rekick_task,
    task_made_progress,
)

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
    return f"ladder-{uuid4().hex[:12]}"


# --- helpers ----------------------------------------------------------------


def _backdate_heartbeat(conn, task_id, seconds=300):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
                (seconds, task_id),
            )


def _set(conn, task_id, **cols):
    """Direct column set for test setup (nudged_at/no_progress_rekicks/retries)."""
    sets, params = [], []
    for k, v in cols.items():
        if k == "nudged_at_ago_s":  # set nudged_at to now() - v seconds
            sets.append("nudged_at = now() - make_interval(secs => %s)")
            params.append(v)
        else:
            sets.append(f"{k} = %s")
            params.append(v)
    params.append(task_id)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s", params)


def _row(conn, task_id, *cols):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM tasks WHERE id = %s", (task_id,))
        r = cur.fetchone()
    conn.commit()
    return r


def _types(conn, task_id):
    return [e.type for e in read_events(conn, task_id=task_id)]


# --- Rung 1: nudge + grace --------------------------------------------------


def test_nudge_then_recovery_preserves_progress(conn, ws):
    """A stalled task is NUDGED (not re-kicked) within the grace window; when the
    worker heartbeats again the nudge episode clears — NO reset, claim preserved."""
    t = enqueue_task(conn, workstream=ws, type="work.stall")
    claim_task(conn, worker_id="w1", workstream=ws)  # → in_progress, watermark set
    _backdate_heartbeat(conn, t.id)

    res = sweep(conn, threshold_s=60, max_retries=5, nudge_grace_s=45)
    assert t.id in res.nudged and t.id not in res.rekicked

    r = _row(conn, t.id, "status", "claimed_by", "retries", "nudged_at")
    assert r["status"] == "in_progress"      # NOT reset
    assert r["claimed_by"] == "w1"           # claim preserved (progress kept)
    assert r["retries"] == 0                 # no retry burned
    assert r["nudged_at"] is not None        # episode open
    assert "task.nudge" in _types(conn, t.id)

    # The worker re-heartbeats within the grace → episode clears, no reset.
    assert heartbeat(conn, t.id, "w1") is not None
    r2 = _row(conn, t.id, "status", "nudged_at", "retries")
    assert r2["status"] == "in_progress" and r2["nudged_at"] is None and r2["retries"] == 0


def test_nudge_grace_elapsed_then_rekick(conn, ws):
    """A nudged task still stale after the grace window IS re-kicked (dead worker
    falls through the nudge)."""
    t = enqueue_task(conn, workstream=ws, type="work.stall")
    claim_task(conn, worker_id="w1", workstream=ws)
    _backdate_heartbeat(conn, t.id)

    # First sweep nudges.
    assert t.id in sweep(conn, threshold_s=60, max_retries=5, nudge_grace_s=45).nudged
    # Grace elapses (simulate by advancing the sweep clock past nudged_at + grace).
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    res2 = sweep(conn, threshold_s=60, max_retries=5, nudge_grace_s=45,
                 stuck_threshold=99, now=future)
    assert t.id in res2.rekicked
    r = _row(conn, t.id, "status", "retries", "nudged_at")
    assert r["status"] == "up_for_grabs" and r["retries"] == 1
    assert r["nudged_at"] is None  # episode closed on re-kick
    assert "task.rekicked" in _types(conn, t.id)


# --- Progress detector ------------------------------------------------------


def test_progress_detector_and_counter(conn, ws):
    """task_made_progress reflects real work since the watermark; a re-kick with NO
    progress increments no_progress_rekicks, one WITH progress resets it to 0."""
    # No-progress task: claim (watermark set), no model.call / steps since.
    a = enqueue_task(conn, workstream=ws, type="work.a")
    claim_task(conn, worker_id="w1", workstream=ws, filter={"type": "work.a"})
    assert task_made_progress(conn, a.id) is False
    rekick_task(conn, a.id, made_progress=task_made_progress(conn, a.id))
    assert _row(conn, a.id, "no_progress_rekicks")["no_progress_rekicks"] == 1

    # Re-claim and stall again with STILL no progress → increments to 2.
    claim_task(conn, worker_id="w2", workstream=ws, filter={"type": "work.a"})
    rekick_task(conn, a.id, made_progress=task_made_progress(conn, a.id))
    assert _row(conn, a.id, "no_progress_rekicks")["no_progress_rekicks"] == 2

    # Progress task: a model.call event after the watermark counts as progress.
    b = enqueue_task(conn, workstream=ws, type="work.b")
    claim_task(conn, worker_id="w3", workstream=ws, filter={"type": "work.b"})
    _set(conn, b.id, no_progress_rekicks=3)  # pretend it had a no-progress history
    append_event(conn, make_event(workstream=ws, type=EVENT_MODEL_CALL, task_id=b.id,
                                  payload={"model": "m", "input_tokens": 10}))
    assert task_made_progress(conn, b.id) is True
    rekick_task(conn, b.id, made_progress=task_made_progress(conn, b.id))
    assert _row(conn, b.id, "no_progress_rekicks")["no_progress_rekicks"] == 0  # reset


# --- Rung 3: escalate-to-PM (stuck), EARLY ----------------------------------


def test_stuck_escalation_supersedes_and_exits_early(conn, ws):
    """no_progress_rekicks at the threshold → the sweep emits task.stuck + supersedes
    (abandoned, reason stuck_needs_replan) and does NOT re-kick — proving it bails
    EARLY, before max_retries."""
    t = enqueue_task(conn, workstream=ws, type="work.stuck")
    claim_task(conn, worker_id="w1", workstream=ws)
    _backdate_heartbeat(conn, t.id)
    # 2 prior no-progress re-kicks, grace already elapsed, retries WELL below max.
    _set(conn, t.id, no_progress_rekicks=2, retries=2, nudged_at_ago_s=120)

    res = sweep(conn, threshold_s=60, max_retries=5, nudge_grace_s=45, stuck_threshold=2)
    assert t.id in res.stuck
    assert t.id not in res.rekicked and t.id not in res.failed  # STOPPED re-kicking

    r = _row(conn, t.id, "status", "result", "stall_reason", "retries")
    assert r["status"] == "abandoned"
    assert r["result"]["reason"] == "stuck_needs_replan"
    assert r["stall_reason"] == "no_progress"
    assert r["retries"] == 2 and r["retries"] < 5  # bailed BEFORE exhausting retries

    types = _types(conn, t.id)
    assert "task.stuck" in types and types.count("task.rekicked") == 0


def test_stuck_event_is_body_free(conn, ws):
    """task.stuck carries only ids/status + reason code + counts — never body text."""
    t = enqueue_task(conn, workstream=ws, type="work.stuck")
    claim_task(conn, worker_id="w1", workstream=ws)
    escalate_stuck_task(conn, t.id, stall_reason="no_progress",
                        no_progress_rekicks=2, retries=2)
    ev = [e for e in read_events(conn, task_id=t.id) if e.type == "task.stuck"][0]
    assert set(ev.payload) <= {"status", "stall_reason", "no_progress_rekicks", "retries"}
    assert ev.payload["stall_reason"] == "no_progress"
    assert ev.payload["no_progress_rekicks"] == 2


# --- Rung 4: abandon backstop for a progressing-but-slow task ---------------


def test_progressing_but_slow_task_hits_max_retries_backstop(conn, ws):
    """A task that keeps making progress (no_progress_rekicks below threshold) but
    never finishes still hits the max_retries force-abandon backstop — NOT stuck."""
    t = enqueue_task(conn, workstream=ws, type="work.slow")
    claim_task(conn, worker_id="w1", workstream=ws)
    _backdate_heartbeat(conn, t.id)
    # Progressing (no_progress_rekicks=0) but retries exhausted; grace elapsed.
    _set(conn, t.id, no_progress_rekicks=0, retries=5, nudged_at_ago_s=120)

    res = sweep(conn, threshold_s=60, max_retries=5, nudge_grace_s=45, stuck_threshold=2)
    assert t.id in res.failed and t.id not in res.stuck
    r = _row(conn, t.id, "status")
    assert r["status"] == "abandoned"
    types = _types(conn, t.id)
    assert "task.failed_exhausted" in types and "task.stuck" not in types
