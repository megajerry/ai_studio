"""Approval loop tests — the human-in-the-loop grant path for 🔴 actions.

Pure tests (fingerprint) run anywhere; the rest exercise the FULL loop against a
real Postgres and SKIP cleanly when none is reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_approvals_db.py

Covered end-to-end (live DB):
- 🔴 with no grant → PENDING, pending row created, tool NOT executed, task blocked.
- resolve(approved) → resume_approved re-queues → worker retry → invoke finds
  grant → tool EXECUTES, grant consumed, task done; a second run pends again (one-shot).
- resolve(denied) → blocked task failed, tool never executes.
- idempotent request (no duplicate pending); events leak no arg/secret values.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from runtime import db
from runtime.approvals import (
    STATUS_APPROVED,
    STATUS_CONSUMED,
    STATUS_DENIED,
    STATUS_PENDING,
    compute_fingerprint,
    consume_grant,
    find_grant,
    get_approval,
    pending_approvals,
    pending_digest,
    request_approval,
    resolve_approval,
)
from runtime.capabilities import ActionTier, Capability
from runtime.enforce import (
    DbEventSink,
    InvokeStatus,
    MemoryEventSink,
    invoke,
)
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.policy import PolicyConfig, load_policy
from runtime.tasks import enqueue_task
from runtime.tools import FilesystemTool, ToolRegistry
from runtime.worker import resume_approved, run_once

# --- pure (no DB) -----------------------------------------------------------


def test_fingerprint_is_stable_and_order_independent():
    tid = uuid4()
    a = compute_fingerprint(tid, "filesystem", ["fs.write", "fs.read"])
    b = compute_fingerprint(tid, "filesystem", ["fs.read", "fs.write"])  # reordered
    assert a == b  # capability order must not change the fingerprint
    assert a == compute_fingerprint(tid, "filesystem", ["fs.write", "fs.read"])


def test_fingerprint_distinguishes_task_tool_caps():
    t1, t2 = uuid4(), uuid4()
    base = compute_fingerprint(t1, "filesystem", ["fs.write"])
    assert base != compute_fingerprint(t2, "filesystem", ["fs.write"])  # task
    assert base != compute_fingerprint(t1, "shell", ["fs.write"])  # tool
    assert base != compute_fingerprint(t1, "filesystem", ["fs.delete"])  # caps


# --- live DB fixtures -------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0007 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-appr-{uuid4().hex[:10]}"


def _red_write_config() -> PolicyConfig:
    """Policy where the Executor's fs.write is re-tiered 🔴 → NEEDS_APPROVAL.

    Rules-as-data: an override, not a code change — the cleanest way to drive a
    real, reversible 🔴 tool call (a scratch write) through the whole loop.
    """
    base = load_policy()
    return base.model_copy(
        update={"tier_overrides": {**base.tier_overrides, Capability.FS_WRITE: ActionTier.RED}}
    )


def _registry(tmp_path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(tmp_path)))
    return reg


# --- approvals store: state machine -----------------------------------------


def test_request_is_idempotent_per_fingerprint(conn, ws):
    sink = DbEventSink(conn)
    tid = uuid4()
    a1 = request_approval(
        conn, task_id=tid, role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
    )
    a2 = request_approval(
        conn, task_id=tid, role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
    )
    assert a1.id == a2.id  # no duplicate pending row for the same action
    assert len([a for a in pending_approvals(conn) if a.id == a1.id]) == 1
    # Only ONE approval.requested event for the deduped request.
    reqs = [e for e in read_events(conn, task_id=tid) if e.type == "approval.requested"]
    assert len(reqs) == 1


def test_resolve_approve_then_grant_consumed_once(conn, ws):
    sink = DbEventSink(conn)
    tid = uuid4()
    fp = compute_fingerprint(tid, "filesystem", ["fs.write"])
    a = request_approval(
        conn, task_id=tid, role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
    )
    assert find_grant(conn, fp) is None  # pending is not a grant

    resolved = resolve_approval(conn, a.id, STATUS_APPROVED, "tester", sink, workstream=ws)
    assert resolved is not None and resolved.status == STATUS_APPROVED
    assert resolved.resolver == "tester" and resolved.resolved_at is not None

    grant = find_grant(conn, fp)
    assert grant is not None and grant.id == a.id  # now a live grant
    consumed = consume_grant(conn, a.id)
    assert consumed is not None and consumed.status == STATUS_CONSUMED
    # One-shot: no grant remains, and a second consume matches nothing.
    assert find_grant(conn, fp) is None
    assert consume_grant(conn, a.id) is None


def test_resolve_deny_leaves_no_grant(conn, ws):
    sink = DbEventSink(conn)
    tid = uuid4()
    fp = compute_fingerprint(tid, "filesystem", ["fs.write"])
    a = request_approval(
        conn, task_id=tid, role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
    )
    denied = resolve_approval(conn, a.id, STATUS_DENIED, "tester", sink, workstream=ws)
    assert denied is not None and denied.status == STATUS_DENIED
    assert find_grant(conn, fp) is None  # a denial never becomes a grant


def test_resolve_is_guarded_to_pending(conn, ws):
    sink = DbEventSink(conn)
    a = request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
    )
    assert resolve_approval(conn, a.id, STATUS_APPROVED, "t", sink, workstream=ws) is not None
    # Already resolved → cannot re-decide (no flip approved→denied).
    assert resolve_approval(conn, a.id, STATUS_DENIED, "t", sink, workstream=ws) is None


def test_resolve_missing_returns_none(conn, ws):
    assert resolve_approval(conn, uuid4(), STATUS_APPROVED, "t", DbEventSink(conn), workstream=ws) is None


def test_pending_digest_batches(conn, ws):
    sink = DbEventSink(conn)
    for _ in range(3):
        request_approval(
            conn, task_id=uuid4(), role="operator", tool="filesystem",
            capabilities=["fs.write"], tier="red", reason="🔴", sink=sink, workstream=ws,
        )
    digest = pending_digest(conn)
    assert digest.count >= 3
    assert digest.by_tier.get("red", 0) >= 3
    assert len(digest.items) == digest.count


# --- enforce.invoke: gate + grant -------------------------------------------


def test_invoke_red_no_grant_pends_and_does_not_execute(conn, ws, tmp_path):
    reg = _registry(tmp_path)
    sink = DbEventSink(conn)
    tid = uuid4()
    res = invoke(
        role="executor", tool_name="filesystem", registry=reg,
        config=_red_write_config(), events=sink, conn=conn, workstream=ws, task_id=tid,
        op="write", path="secret.txt", content="TOP_SECRET_VALUE",
    )
    assert res.status is InvokeStatus.PENDING
    assert res.result is None
    assert res.approval_id is not None
    # Tool NOT executed — file must not exist.
    assert not (tmp_path / "secret.txt").exists()
    # A pending row was persisted for this action.
    a = get_approval(conn, res.approval_id)
    assert a is not None and a.status == STATUS_PENDING and a.task_id == tid


def test_invoke_with_grant_executes_consumes_and_is_one_shot(conn, ws, tmp_path):
    reg = _registry(tmp_path)
    cfg = _red_write_config()
    sink = DbEventSink(conn)
    tid = uuid4()
    args = dict(op="write", path="art.txt", content="studio-ok-content")

    # 1. No grant → pend.
    r1 = invoke(role="executor", tool_name="filesystem", registry=reg, config=cfg,
                events=sink, conn=conn, workstream=ws, task_id=tid, **args)
    assert r1.status is InvokeStatus.PENDING
    assert not (tmp_path / "art.txt").exists()

    # 2. Human grants it.
    resolve_approval(conn, r1.approval_id, STATUS_APPROVED, "tester", sink, workstream=ws)

    # 3. Retry (same task+tool+caps) → invoke finds the grant → EXECUTES.
    r2 = invoke(role="executor", tool_name="filesystem", registry=reg, config=cfg,
                events=sink, conn=conn, workstream=ws, task_id=tid, **args)
    assert r2.status is InvokeStatus.EXECUTED
    assert r2.approval_id == r1.approval_id
    assert (tmp_path / "art.txt").read_text() == "studio-ok-content"
    # Grant is now consumed.
    assert get_approval(conn, r1.approval_id).status == STATUS_CONSUMED
    # tool.invoked event notes which grant authorized it.
    invoked = [e for e in read_events(conn, task_id=tid) if e.type == "tool.invoked"]
    assert invoked and invoked[-1].payload.get("approval_id") == str(r1.approval_id)

    # 4. One-shot: a second identical call with no fresh grant PENDs again.
    r3 = invoke(role="executor", tool_name="filesystem", registry=reg, config=cfg,
                events=sink, conn=conn, workstream=ws, task_id=tid, **args)
    assert r3.status is InvokeStatus.PENDING
    assert r3.approval_id != r1.approval_id  # a brand-new pending request


def test_events_carry_identity_not_secrets(conn, ws, tmp_path):
    reg = _registry(tmp_path)
    sink = DbEventSink(conn)
    tid = uuid4()
    res = invoke(
        role="executor", tool_name="filesystem", registry=reg,
        config=_red_write_config(), events=sink, conn=conn, workstream=ws, task_id=tid,
        op="write", path="x.txt", content="DO_NOT_LOG_THIS_SECRET",
    )
    resolve_approval(conn, res.approval_id, STATUS_APPROVED, "tester", sink, workstream=ws)

    events = read_events(conn, task_id=tid)
    req = next(e for e in events if e.type == "approval.requested")
    resv = next(e for e in events if e.type == "approval.resolved")
    for ev in (req, resv):
        blob = str(ev.payload)
        assert "DO_NOT_LOG_THIS_SECRET" not in blob  # no arg/secret values
        assert "content" not in ev.payload  # no arg values at all
    # But identity IS present for auditing.
    assert req.payload["role"] == "executor" and req.payload["tool"] == "filesystem"
    assert req.payload["tier"] == "red" and req.payload["reason"]
    assert resv.payload["status"] == "approved" and resv.payload["resolver"] == "tester"


# --- worker end-to-end: block → grant → resume → retry → execute → done -----


def _drive_to_blocked(conn, ws, tmp_path, worker_id="appr-w"):
    """Enqueue a work task whose 🔴 write pends; run it → task parked `blocked`."""
    cfg = _red_write_config()
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(tmp_path)))
    sink = DbEventSink(conn)
    task = enqueue_task(
        conn, workstream=ws, type="work.demo",
        payload={"goal": "g", "criterion": "artifact contains marker",
                 "marker": f"studio-ok:{uuid4().hex[:6]}", "attempt": 1},
    )
    r = run_once(conn, worker_id, sink, registry=reg, config=cfg, workstream=ws)
    return task, r, reg, cfg, sink


def test_worker_block_then_approve_resume_execute_done(conn, ws, tmp_path):
    task, r, reg, cfg, sink = _drive_to_blocked(conn, ws, tmp_path)
    # Parked blocked, tool not executed (no artifact yet).
    assert r is not None and r.kind == "work" and r.outcome == "blocked"
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM tasks WHERE id = %s", (task.id,))
        row = cur.fetchone()
    conn.commit()
    assert row["status"] == "blocked"
    approval_id = row["result"]["blocked_on_approval"]
    assert get_approval(conn, approval_id).status == STATUS_PENDING
    assert not list(tmp_path.glob("work-*.txt"))  # nothing written while blocked

    # Human approves.
    resolve_approval(conn, approval_id, STATUS_APPROVED, "tester", sink, workstream=ws)

    # resume_approved re-queues the blocked task.
    resumed = resume_approved(conn, sink)
    assert str(task.id) in resumed.resumed
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (task.id,))
        assert cur.fetchone()["status"] == "queued"
    conn.commit()

    # Worker retry: invoke finds the grant → executes → verify → done.
    r2 = run_once(conn, "appr-w", sink, registry=reg, config=cfg, workstream=ws)
    assert r2 is not None and r2.kind == "work" and r2.outcome == "done"
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (task.id,))
        assert cur.fetchone()["status"] == "done"
    conn.commit()
    assert get_approval(conn, approval_id).status == STATUS_CONSUMED  # grant spent
    assert list(tmp_path.glob("work-*.txt"))  # artifact now exists

    # Event trail proves the full loop landed in the append-only log.
    types = [e.type for e in read_events(conn, workstream=ws)]
    for required in ("approval.requested", "approval.resolved", "approval.resumed",
                     "tool.invoked", "task.finished"):
        assert required in types, f"missing {required} in {types}"


def test_worker_block_then_deny_fails_task_no_execution(conn, ws, tmp_path):
    task, r, reg, cfg, sink = _drive_to_blocked(conn, ws, tmp_path, worker_id="appr-w2")
    assert r is not None and r.outcome == "blocked"
    with conn.cursor() as cur:
        cur.execute("SELECT result FROM tasks WHERE id = %s", (task.id,))
        approval_id = cur.fetchone()["result"]["blocked_on_approval"]
    conn.commit()

    # Human denies.
    resolve_approval(conn, approval_id, STATUS_DENIED, "tester", sink, workstream=ws)

    # resume_approved fails the blocked task; the tool never executes.
    res = resume_approved(conn, sink)
    assert str(task.id) in res.failed
    with conn.cursor() as cur:
        cur.execute("SELECT status, result FROM tasks WHERE id = %s", (task.id,))
        row = cur.fetchone()
    conn.commit()
    assert row["status"] == "failed"
    assert row["result"]["approved"] is False
    assert not list(tmp_path.glob("work-*.txt"))  # nothing was ever written
    assert get_approval(conn, approval_id).status == STATUS_DENIED
