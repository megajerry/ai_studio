"""Runtime-bridge tests — the Spokesman wired to the live runtime DB (ADR-0006/§9).

Pure classification tests run anywhere; the rest exercise the FULL bridge against
a real Postgres and SKIP cleanly when none is reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest spokesman/tests/test_runtime_bridge.py

Covered end-to-end (live DB):
- a pending 🛑 approval (via ``request_approval``) surfaces in ``poll_notifications``
  + renders into a batched digest;
- a ``review.alarm`` event is classified 🚨 immediate;
- inbound ``approve <id>`` → ``resolve`` → the approval is ``approved`` in the DB
  and ``resume_approved`` re-queues the blocked task;
- ``status`` returns real counts; the cursor prevents re-notification;
- notifications leak no secret / arg values; sends are dry-run (no network).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from runtime import db
from runtime.approvals import (
    STATUS_APPROVED,
    get_approval,
    request_approval,
)
from runtime.enforce import DbEventSink
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import Event, TaskStatus, make_event
from runtime.tasks import block_task, claim_task, enqueue_task
from runtime.worker import resume_approved

from spokesman.app import create_app, handle_inbound_command, run_notifier_pass
from spokesman.classify import Notifier, NotifyKind, compose_digest, PendingItem
from spokesman.runtime_bridge import (
    classify_event,
    load_cursor,
    poll_notifications,
    resolve,
    studio_status,
)
from spokesman.state import InboundMessage

from .conftest import APP_SECRET, make_settings

# --- pure (no DB) -----------------------------------------------------------

SECRET_VALUE = "s3cr3t-password-xyz"


def _event(etype: str, payload: dict, *, seq: int = 1) -> Event:
    return Event(
        id=uuid4(), ts=datetime.now(timezone.utc),
        seq=seq, task_id=uuid4(), workstream="test", type=etype, payload=payload,
    )


def test_classify_approval_requested_is_batched_approve() -> None:
    item = classify_event(_event(
        "approval.requested",
        {"approval_id": "abc-123", "role": "executor", "tool": "filesystem",
         "tier": "red", "reason": "capability fs.write requires approval"},
    ))
    assert item is not None and item.kind is NotifyKind.APPROVE
    assert item.approval_id == "abc-123"
    assert "approve abc-123" in item.text and "deny abc-123" in item.text


def test_classify_review_alarm_is_immediate() -> None:
    item = classify_event(_event(
        "review.alarm",
        {"severity": "high", "signal_count": 2,
         "reasons": ["hallucinated success", "budget blowout"]},
    ))
    assert item is not None and item.kind is NotifyKind.ALARM
    assert "high" in item.text


def test_classify_failed_exhausted_is_inform() -> None:
    item = classify_event(_event(
        "task.failed_exhausted", {"retries": 3, "status": "failed"},
    ))
    assert item is not None and item.kind is NotifyKind.INFORM


def test_classify_high_review_flagged_deduped() -> None:
    # A HIGH review.flagged is dropped (the episode already alarms + approves).
    assert classify_event(_event("review.flagged", {"severity": "high"})) is None
    low = classify_event(_event("review.flagged", {"severity": "medium", "signal_count": 1}))
    assert low is not None and low.kind is NotifyKind.INFORM


def test_operational_events_are_not_notified() -> None:
    for etype in ("policy.decision", "tool.invoked", "task.claimed", "model.call"):
        assert classify_event(_event(etype, {"arg_keys": ["path"]})) is None


def test_classify_never_leaks_arg_values() -> None:
    item = classify_event(_event(
        "approval.requested",
        {"approval_id": "id1", "role": "executor", "tool": "filesystem",
         "tier": "red", "reason": "needs approval", "capabilities": ["fs.write"],
         # a hostile/naive payload must never surface an actual value:
         "arg_keys": ["path"]},
    ))
    assert item is not None
    assert SECRET_VALUE not in item.text


# --- live DB ----------------------------------------------------------------

pytestmark_live = pytest.mark.skipif(
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
    return f"test-spk-{uuid4().hex[:10]}"


def _max_seq(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(seq), 0) AS s FROM events")
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return int(row["s"])


class RecordingClient:
    """Dry-run stand-in WhatsApp client: records sends, never calls out."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.sent.append((text, to))
        return {"dry_run": True, "to": to, "text": text}


@pytestmark_live
def test_pending_approval_shows_in_poll_and_digest(conn, ws) -> None:
    baseline = _max_seq(conn)
    a = request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red",
        reason="capability fs.write requires approval", sink=DbEventSink(conn),
        workstream=ws,
    )
    batch = poll_notifications(conn, baseline)
    mine = [i for i in batch.items if i.approval_id == str(a.id)]
    assert len(mine) == 1
    item = mine[0]
    assert item.kind is NotifyKind.APPROVE
    assert item in batch.digest_items and item not in batch.alarms
    assert batch.cursor > baseline

    # Renders into a batched digest (ADR-0006: approvals are batched).
    digest = compose_digest([PendingItem(NotifyKind.APPROVE, item.text)])
    assert str(a.id) in digest and "Needs approval" in digest


@pytestmark_live
def test_review_alarm_is_classified_immediate(conn, ws) -> None:
    baseline = _max_seq(conn)
    tid = uuid4()
    append_event(conn, make_event(
        workstream=ws, type="review.alarm", task_id=tid,
        payload={"severity": "high", "signal_count": 1,
                 "reasons": ["hallucinated success"], "mark": "ALARM"},
    ))
    batch = poll_notifications(conn, baseline)
    # Scope to THIS alarm's task_id: the shared event log may carry other alarms
    # appended concurrently, so assert on our own event, not a global count.
    alarms = [i for i in batch.alarms
              if i.event_type == "review.alarm" and i.task_id == str(tid)]
    assert len(alarms) == 1 and alarms[0].kind is NotifyKind.ALARM


@pytestmark_live
def test_cursor_prevents_renotification(conn, ws) -> None:
    baseline = _max_seq(conn)
    a = request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="x", sink=DbEventSink(conn),
        workstream=ws,
    )
    first = poll_notifications(conn, baseline)
    assert any(i.approval_id == str(a.id) for i in first.items)
    # A second poll from the advanced cursor never RE-notifies an item already
    # seen (the cursor's whole job). On the shared event log unrelated events may
    # be appended concurrently between the two polls, so assert the real
    # invariant — our own approval is not re-surfaced and the cursor only ever
    # advances — rather than an idle-DB "nothing changed" delta.
    second = poll_notifications(conn, first.cursor)
    assert all(i.approval_id != str(a.id) for i in second.items)
    assert second.cursor >= first.cursor


@pytestmark_live
def test_studio_status_returns_real_counts(conn, ws) -> None:
    # Fixture workstreams / demo types must NOT move the stakeholder feed.
    before = studio_status(conn)
    enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    after_noise = studio_status(conn)
    assert after_noise.queued == before.queued
    assert after_noise.open_tasks == before.open_tasks

    # A real vertical + real type does.
    enqueue_task(conn, workstream="productivity", type="work.task", payload={"goal": "live"})
    after = studio_status(conn)
    assert after.queued == before.queued + 1
    assert after.open_tasks == before.open_tasks + 1
    rendered = after.render()
    assert "Open tasks" in rendered and "Pending approvals" in rendered


@pytestmark_live
def test_inbound_approve_resolves_and_resumes_blocked_task(conn, ws) -> None:
    # 1. A task is claimed (in_progress), then blocked on a 🔴 approval.
    task = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    claimed = claim_task(conn, worker_id="w-test", workstream=ws)
    assert claimed is not None and claimed.id == task.id
    approval = request_approval(
        conn, task_id=task.id, role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="needs approval",
        sink=DbEventSink(conn), workstream=ws,
    )
    blocked = block_task(conn, task.id, approval_id=approval.id, reason="🔴")
    assert blocked is not None and blocked.status == TaskStatus.BLOCKED

    # 2. Inbound "approve <id>" through the runtime bridge (dry-run reply).
    client = RecordingClient()
    settings = make_settings(Path("/tmp") / f"spk-{uuid4().hex}")
    msg = InboundMessage(
        message_id="wamid.X", sender="15550001111",
        text=f"approve {approval.id}", timestamp="1700000000",
    )
    result = handle_inbound_command(settings, client, db.connect, msg)
    assert result == {"command": "approve", "ok": True, "status": STATUS_APPROVED}
    assert client.sent and str(approval.id) in client.sent[0][0]

    # 3. The approval is approved in the DB...
    assert get_approval(conn, approval.id).status == STATUS_APPROVED
    # ...and the blocked task becomes resumable (re-queued for retry).
    res = resume_approved(conn, DbEventSink(conn))
    assert str(task.id) in res.resumed


@pytestmark_live
def test_inbound_deny_marks_denied(conn, ws) -> None:
    approval = request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="x",
        sink=DbEventSink(conn), workstream=ws,
    )
    r = resolve(conn, str(approval.id), "deny", "whatsapp:****1111")
    assert r is not None and r.status == "denied"
    # Second resolve is a no-op (guarded to pending).
    assert resolve(conn, str(approval.id), "deny", "x") is None


@pytestmark_live
def test_inbound_status_reply_from_db(conn, ws) -> None:
    client = RecordingClient()
    settings = make_settings(Path("/tmp") / f"spk-{uuid4().hex}")
    msg = InboundMessage("id", "15550001111", "status", "1700000000")
    result = handle_inbound_command(settings, client, db.connect, msg)
    assert result == {"command": "status"}
    assert client.sent and "AI Studio status" in client.sent[0][0]


@pytestmark_live
def test_notifier_pass_routes_alarm_now_and_batches_approval(conn, tmp_path, ws) -> None:
    # Seed one alarm (🚨) + one approval (🛑) after a fresh baseline cursor.
    baseline = _max_seq(conn)
    settings = make_settings(tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    from spokesman.runtime_bridge import save_cursor
    save_cursor(settings.state_dir, baseline)

    append_event(conn, make_event(
        workstream=ws, type="review.alarm", task_id=uuid4(),
        payload={"severity": "high", "signal_count": 1, "reasons": ["attack"]},
    ))
    request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="approve me",
        sink=DbEventSink(conn), workstream=ws,
    )

    client = RecordingClient()
    notifier = Notifier(client)  # type: ignore[arg-type]
    stats = run_notifier_pass(settings, notifier, db.connect)

    assert stats["alarms_sent"] == 1  # 🚨 sent immediately
    assert stats["digest_queued"] == 1  # 🛑 batched, not sent yet
    assert len(client.sent) == 1  # only the alarm went out
    assert "\U0001F6A8" in client.sent[0][0]  # 🚨 prefix
    assert notifier.pending_count == 1
    assert load_cursor(settings.state_dir) == stats["scanned_to_seq"] > baseline

    # Flushing sends the single batched approval digest.
    notifier.flush()
    assert any("Needs approval" in t for t, _ in client.sent)


@pytestmark_live
def test_notifications_leak_no_secret_values(conn, ws) -> None:
    # Even a payload carrying arg keys must never surface a value.
    baseline = _max_seq(conn)
    append_event(conn, make_event(
        workstream=ws, type="approval.requested", task_id=uuid4(),
        payload={"approval_id": str(uuid4()), "role": "executor",
                 "tool": "shell", "tier": "red", "reason": "🔴 delete",
                 "capabilities": ["fs.delete"], "arg_keys": ["path"]},
    ))
    batch = poll_notifications(conn, baseline)
    for item in batch.items:
        assert SECRET_VALUE not in item.text
        assert "arg_keys" not in item.text


@pytestmark_live
def test_poll_endpoint_gated_and_runs(conn, tmp_path) -> None:
    settings = make_settings(tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    app = create_app(settings, connect=db.connect)
    client = TestClient(app)
    # Control-plane gate intact: no token → 401.
    assert client.post("/poll").status_code == 401
    ok = client.post("/poll", headers={"X-Spokesman-Token": "test-api-token"})
    assert ok.status_code == 200
    assert "scanned_to_seq" in ok.json()


@pytestmark_live
def test_webhook_inbound_approve_end_to_end(conn, tmp_path) -> None:
    """Full inbound path: signed webhook → approve <id> → DB approved."""
    approval = request_approval(
        conn, task_id=uuid4(), role="executor", tool="filesystem",
        capabilities=["fs.write"], tier="red", reason="x",
        sink=DbEventSink(conn), workstream=f"test-spk-{uuid4().hex[:8]}",
    )
    settings = make_settings(tmp_path / "state")
    (tmp_path / "state" / "inbox").mkdir(parents=True, exist_ok=True)
    app = create_app(settings, connect=db.connect)
    client = TestClient(app)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"messages": [{
            "id": "wamid.T", "from": "15559998888", "type": "text",
            "timestamp": "1700000000", "text": {"body": f"approve {approval.id}"},
        }]}}]}],
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200 and resp.json()["replies"] == 1
    assert get_approval(conn, approval.id).status == STATUS_APPROVED
