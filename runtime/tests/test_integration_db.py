"""Integration tests against a real Postgres.

These SKIP cleanly (never error, never hang) when no database is reachable — the
off-host sandbox has none. A short-timeout probe decides at collection time.
Run against a live DB with, e.g.:

    docker compose up -d postgres
    python -m runtime.migrate
    pytest runtime/tests/test_integration_db.py
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.events import append_event, read_events
from runtime.migrate import migrate
from runtime.models import Assignee, TaskStatus, make_event
from runtime.scheduler import PM_TICK_TYPE, tick_once
from runtime.supervisor import sweep
from runtime.tasks import (
    add_spent_tokens,
    claim_task,
    complete_task,
    enqueue_task,
    find_stale_tasks,
    heartbeat,
    rekick_task,
)

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure schema exists
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    # Unique workstream per test → isolation without deleting shared rows.
    return f"test-{uuid4().hex[:12]}"


def test_append_and_read_events(conn, ws):
    e1 = append_event(conn, make_event(workstream=ws, type="a", payload={"n": 1}))
    e2 = append_event(conn, make_event(workstream=ws, type="b", trace_id="tr"))
    got = read_events(conn, workstream=ws)
    assert [e.id for e in got] == [e1.id, e2.id]
    assert got[0].payload == {"n": 1}
    assert got[1].trace_id == "tr"


def test_append_order_preserved_within_one_transaction(conn, ws):
    # Regression: `ts` defaults to now() = transaction start time, so many events
    # appended in a single transaction share an identical `ts`. Ordering by `ts`
    # made their read-back order non-deterministic; ordering by the monotonic
    # `seq` must replay them in true insertion order.
    n = 50
    with conn.transaction():
        appended = [
            append_event(conn, make_event(workstream=ws, type="e", payload={"i": i}))
            for i in range(n)
        ]
    # All share one transaction timestamp, confirming the hazard is exercised.
    assert len({e.ts for e in appended}) == 1
    got = read_events(conn, workstream=ws)
    assert [e.id for e in got] == [e.id for e in appended]
    assert [e.payload["i"] for e in got] == list(range(n))
    # seq is assigned, strictly increasing, and matches insertion order.
    seqs = [e.seq for e in got]
    assert all(s is not None for s in seqs)
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == n


def test_read_events_since_filter(conn, ws):
    e1 = append_event(conn, make_event(workstream=ws, type="a"))
    e2 = append_event(conn, make_event(workstream=ws, type="b"))
    after = read_events(conn, workstream=ws, since=e1.ts)
    assert e2.id in {e.id for e in after}
    assert e1.id not in {e.id for e in after}


def test_enqueue_emits_created_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="build", payload={"x": 1}, priority=5)
    assert t.status is TaskStatus.QUEUED
    events = read_events(conn, task_id=t.id)
    assert [e.type for e in events] == ["task.created"]


def test_claim_picks_highest_priority(conn, ws):
    enqueue_task(conn, workstream=ws, type="low", priority=1)
    hi = enqueue_task(conn, workstream=ws, type="high", priority=9)
    claimed = claim_task(conn, worker_id="w1", workstream=ws)
    assert claimed is not None
    assert claimed.id == hi.id
    assert claimed.status is TaskStatus.IN_PROGRESS
    assert claimed.claimed_by == "w1"
    assert claimed.heartbeat_at is not None
    types = [e.type for e in read_events(conn, task_id=hi.id)]
    assert types == ["task.created", "task.claimed"]


def test_claim_respects_assignee_targeting(conn, ws):
    enqueue_task(conn, workstream=ws, type="host-only", assignee=Assignee.HOST)
    # An offhost worker must not claim a host-targeted task.
    assert claim_task(conn, worker_id="w-off", assignee=Assignee.OFFHOST, workstream=ws) is None
    # A host worker can.
    claimed = claim_task(conn, worker_id="w-host", assignee=Assignee.HOST, workstream=ws)
    assert claimed is not None and claimed.assignee is Assignee.HOST


def test_claim_returns_none_when_empty(conn, ws):
    assert claim_task(conn, worker_id="w1", workstream=ws) is None


def test_heartbeat_only_by_holder(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    assert heartbeat(conn, t.id, "someone-else") is None
    beat = heartbeat(conn, t.id, "w1")
    assert beat is not None and beat.heartbeat_at is not None


def test_heartbeat_emits_no_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    heartbeat(conn, t.id, "w1")
    heartbeat(conn, t.id, "w1")
    # High-frequency liveness must not bloat the append-only log (ADR-0013).
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types == ["task.created", "task.claimed"]
    assert "task.heartbeat" not in types


def test_complete_task_sets_result_and_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    done = complete_task(conn, t.id, result={"ok": True}, spent_tokens=42)
    assert done.status is TaskStatus.DONE
    assert done.result == {"ok": True}
    assert done.spent_tokens == 42
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types[-1] == "task.finished"


def test_complete_rejects_non_terminal_status(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    with pytest.raises(ValueError):
        complete_task(conn, t.id, status=TaskStatus.QUEUED)


def test_complete_guard_rejects_non_in_progress_task(conn, ws):
    # A queued (unclaimed) task cannot be finalized by default; no event emitted.
    t = enqueue_task(conn, workstream=ws, type="t")
    assert complete_task(conn, t.id, result={"x": 1}) is None
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types == ["task.created"]


def test_complete_guard_blocks_double_finalize(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    assert complete_task(conn, t.id, status=TaskStatus.DONE) is not None
    # Second finalize conflicts (already done) → None, no extra finished event.
    assert complete_task(conn, t.id, status=TaskStatus.FAILED) is None
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types.count("task.finished") == 1


def test_complete_force_bypasses_guard(conn, ws):
    # Supervisor force-fails a stale/re-kicked (still queued) task.
    t = enqueue_task(conn, workstream=ws, type="t")
    done = complete_task(
        conn, t.id, status=TaskStatus.FAILED, result={"reason": "stale"}, force=True
    )
    assert done is not None and done.status is TaskStatus.FAILED
    assert done.result == {"reason": "stale"}
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert types[-1] == "task.finished"


def test_complete_missing_task_returns_none(conn, ws):
    assert complete_task(conn, uuid4(), force=True) is None


def test_find_stale_tasks(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    # Backdate the heartbeat to simulate a dropped worker.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET heartbeat_at = now() - interval '300 seconds' WHERE id = %s",
                (t.id,),
            )
    stale_ids = {s.id for s in find_stale_tasks(conn, threshold_seconds=60)}
    assert t.id in stale_ids
    # Not stale under a large threshold.
    assert t.id not in {s.id for s in find_stale_tasks(conn, threshold_seconds=10_000)}


# --- supervisor: re-kick + exhausted-fail (M3a) -----------------------------


def _backdate_heartbeat(conn, task_id, seconds=300):
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET heartbeat_at = now() - make_interval(secs => %s) WHERE id = %s",
                (seconds, task_id),
            )


def test_rekick_resets_task_and_emits_event(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    kicked = rekick_task(conn, t.id)
    assert kicked is not None
    assert kicked.status is TaskStatus.QUEUED
    assert kicked.claimed_by is None
    assert kicked.heartbeat_at is None
    assert kicked.retries == 1
    assert [e.type for e in read_events(conn, task_id=t.id)][-1] == "task.rekicked"


def test_rekick_guarded_to_in_progress(conn, ws):
    # A queued (unclaimed) task is not re-kicked; no event, no counter bump.
    t = enqueue_task(conn, workstream=ws, type="t")
    assert rekick_task(conn, t.id) is None
    assert [e.type for e in read_events(conn, task_id=t.id)] == ["task.created"]


def test_sweep_rekicks_then_force_fails_at_max(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)

    # First stale sweep re-kicks (retries 0 -> 1).
    _backdate_heartbeat(conn, t.id)
    res1 = sweep(conn, threshold_s=60, max_retries=1)
    assert t.id in res1.rekicked

    # Re-claim, go stale again; now retries (1) >= max (1) → force-failed.
    claim_task(conn, worker_id="w2", workstream=ws)
    _backdate_heartbeat(conn, t.id)
    res2 = sweep(conn, threshold_s=60, max_retries=1)
    assert t.id in res2.failed

    with conn.cursor() as cur:
        cur.execute("SELECT status, retries FROM tasks WHERE id = %s", (t.id,))
        row = cur.fetchone()
    conn.commit()
    assert row["status"] == "failed"
    types = [e.type for e in read_events(conn, task_id=t.id)]
    # Both the re-kick and the exhausted-fail are traceable in the log; the fail
    # also emits task.finished (via complete_task). (task.finished and
    # task.failed_exhausted share one transaction timestamp, so their relative
    # order is not asserted.)
    assert "task.rekicked" in types
    assert "task.failed_exhausted" in types
    assert "task.finished" in types


def test_sweep_does_not_clobber_self_completed_task(conn, ws):
    # Fix (b): a task at max retries that self-completes in the scan->write window
    # must NOT be force-failed over its terminal state. The supervisor's scan sees
    # it in_progress (snapshot), but the worker finishes it before the sweep writes;
    # the guarded exhausted-fail must then skip it, leaving `done` intact.
    t = enqueue_task(conn, workstream=ws, type="t")
    claimed = claim_task(conn, worker_id="w1", workstream=ws)
    assert claimed is not None and claimed.id == t.id

    # The scan snapshot: in_progress, retries already at max (would force-fail).
    stale_snapshot = claimed.model_copy(update={"retries": 5})

    # The worker self-completes just before the sweep acts on the snapshot.
    done = complete_task(conn, t.id, status=TaskStatus.DONE, result={"ok": True})
    assert done is not None and done.status is TaskStatus.DONE

    # Feed the pre-completion snapshot to the sweep (models the race precisely).
    res = sweep(conn, threshold_s=60, max_retries=5, find_stale=lambda c, s: [stale_snapshot])
    assert stale_snapshot.id not in res.failed  # skipped, not clobbered

    # The row is still `done` with its result untouched.
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM tasks WHERE id = %s", (t.id,))
        row = cur.fetchone()
    conn.commit()
    assert row["status"] == "done"
    assert row["result"] == {"ok": True}

    # No spurious exhausted-fail event; the finish stays the last state change.
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert "task.failed_exhausted" not in types
    assert types[-1] == "task.finished"


# --- scheduler: PM pulse enqueue-vs-skip (M3a) ------------------------------


def test_tick_once_enqueues_then_skips(conn, ws):
    first = tick_once(conn, workstream=ws)
    assert first is not None and first.type == PM_TICK_TYPE
    # A pm.tick is now queued → second tick is skipped (no pileup).
    assert tick_once(conn, workstream=ws) is None
    # Finalize it; then a fresh tick is allowed again.
    claim_task(conn, worker_id="pm", workstream=ws)
    complete_task(conn, first.id, status=TaskStatus.DONE)
    assert tick_once(conn, workstream=ws) is not None


# --- model-call spent-token accounting (M3b) --------------------------------


def test_add_spent_tokens_accumulates(conn, ws):
    t = enqueue_task(conn, workstream=ws, type="t")
    assert add_spent_tokens(conn, t.id, 100).spent_tokens == 100
    assert add_spent_tokens(conn, t.id, 50).spent_tokens == 150
    # Emits no event — the model.call event already carries per-call tokens.
    assert [e.type for e in read_events(conn, task_id=t.id)] == ["task.created"]


def test_add_spent_tokens_missing_task_returns_none(conn):
    assert add_spent_tokens(conn, uuid4(), 10) is None


def test_call_model_records_spend_and_event(conn, ws, monkeypatch):
    # End-to-end keyless: route → dry-run complete → cost → model.call event →
    # task.spent_tokens increment, all against a live DB via DbEventSink.
    from runtime.enforce import DbEventSink
    from runtime.model.call import call_model

    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "MODELS_DRY_RUN"):
        monkeypatch.delenv(env, raising=False)

    t = enqueue_task(conn, workstream=ws, type="t")
    claim_task(conn, worker_id="w1", workstream=ws)
    sink = DbEventSink(conn)
    comp = call_model(
        "exec",
        "execute",
        [{"role": "user", "content": "do the thing " * 30}],
        workstream=ws,
        registry=None,
        sink=sink,
        task_id=t.id,
        conn=conn,
    )
    assert comp.provider == "dryrun"
    types = [e.type for e in read_events(conn, task_id=t.id)]
    assert "model.routed" in types and "model.call" in types
    with conn.cursor() as cur:
        cur.execute("SELECT spent_tokens FROM tasks WHERE id = %s", (t.id,))
        spent = cur.fetchone()["spent_tokens"]
    conn.commit()
    assert spent == comp.usage.total_tokens


# --- worker: full agent-driven loop end-to-end (M3c) ------------------------


def test_worker_full_loop_pm_to_done(conn, ws, tmp_path, monkeypatch):
    """Against a live DB: tick → PM plans + enqueues work → Executor + Verifier →
    committed done, with the canonical event trail landing in the M1 log."""
    from runtime.enforce import DbEventSink
    from runtime.policy import load_policy
    from runtime.worker import build_registry, run_once

    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    registry = build_registry(str(tmp_path))
    config = load_policy()
    sink = DbEventSink(conn)

    tick = tick_once(conn, workstream=ws)
    assert tick is not None and tick.type == PM_TICK_TYPE

    r1 = run_once(conn, "it-worker", sink, registry=registry, config=config, workstream=ws)
    assert r1 is not None and r1.kind == "pm" and r1.outcome == "done"

    # The PM decomposed the goal into N>1 real work tasks in the live queue.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
            (ws,),
        )
        n_work = cur.fetchone()["n"]
    conn.commit()
    assert n_work > 1, f"expected decomposition into >1 work items, got {n_work}"

    # Drain every decomposed work item → each reaches verified done.
    done = 0
    while True:
        r = run_once(conn, "it-worker", sink, registry=registry, config=config, workstream=ws)
        if r is None:
            break
        assert r.kind == "work" and r.outcome == "done"
        done += 1
    assert done == n_work

    types = [e.type for e in read_events(conn, workstream=ws)]
    for required in ("task.created", "pm.planned", "model.routed", "model.call",
                     "policy.decision", "tool.invoked", "task.finished"):
        assert required in types, f"missing {required} in {types}"


def test_pm_pushback_creates_approval_and_enqueues_no_work(conn, ws, monkeypatch):
    """Against a live DB: an INFEASIBLE plan makes the PM push back — it raises a
    real 🛑 approval row and enqueues NO work (the confidence gate's pushback arm)."""
    from runtime.approvals import STATUS_PENDING, pending_approvals
    from runtime.enforce import DbEventSink
    from runtime.roles.pm import run_pm_tick

    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    sink = DbEventSink(conn)
    tick = tick_once(conn, workstream=ws)
    assert tick is not None
    claimed = claim_task(conn, worker_id="pm", workstream=ws)
    assert claimed is not None

    infeasible = {
        "restated_goal": "delete all of production",
        "confidence": 0.95, "feasible": False,
        "reason": "destructive + out of scope", "work_items": [],
    }
    plan = run_pm_tick(
        conn, claimed, sink,
        call_model=lambda **kw: type("C", (), {"text": __import__("json").dumps(infeasible)})(),
    )
    assert plan.decision == "pushback" and plan.approval_id

    # No work.* task was enqueued for this workstream.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
            (ws,),
        )
        assert cur.fetchone()["n"] == 0
    conn.commit()

    # A real 🛑 approval row exists, pending, for this pm.tick.
    pend = [a for a in pending_approvals(conn) if a.task_id == claimed.id]
    assert pend and pend[0].tier == "🛑" and pend[0].status == STATUS_PENDING
    assert pend[0].role == "pm"

    types = [e.type for e in read_events(conn, workstream=ws)]
    assert "pm.pushback" in types and "approval.requested" in types
    assert "pm.planned" not in types
