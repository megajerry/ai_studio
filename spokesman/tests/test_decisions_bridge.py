"""Spokesman decision-channel tests — async open-ended decisions (ADR-0025).

The Spokesman side of the decision primitive (test_runtime_bridge.py's sibling):
a ``decision.requested`` becomes a batched 🛑 digest item rendering question +
options + default (leak-free, read from the row), and an inbound ``decide <id>
<answer>`` answers it via the runtime store (which resumes the parked task). Pure
classification runs anywhere; the DB-backed tests SKIP when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.decisions import STATUS_ANSWERED, get_decision, request_decision
from runtime.enforce import DbEventSink
from runtime.migrate import migrate
from runtime.models import Event, TaskStatus
from runtime.tasks import claim_task, enqueue_task, get_task

from spokesman.app import handle_inbound_command
from spokesman.classify import NotifyKind, PendingItem, compose_digest
from spokesman.runtime_bridge import classify_event, poll_notifications
from spokesman.state import InboundMessage

from .conftest import make_settings

# --- pure (no DB) -----------------------------------------------------------


def _event(etype: str, payload: dict, *, seq: int = 1) -> Event:
    return Event(
        id=uuid4(), ts=datetime.now(timezone.utc),
        seq=seq, task_id=uuid4(), workstream="test", type=etype, payload=payload,
    )


def test_classify_decision_requested_is_batched_approve_body_free() -> None:
    """Without a conn, a body-free decision.requested still classifies 🛑 batched.

    The event carries NO question text, so the pure render degrades to id + shape +
    the reply hint — never dropped, never leaking (there is nothing to leak)."""
    did = "dec-abc-123"
    item = classify_event(_event(
        "decision.requested",
        {"decision_id": did, "workstream": "test", "seq": 7,
         "has_options": True, "has_default": True},
    ))
    assert item is not None and item.kind is NotifyKind.APPROVE
    assert item.decision_id == did
    assert f"decide {did}" in item.text          # drives the inbound verb
    assert "Decision needed" in item.text


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
    # NON-noise unique workstream (real prefix + non-hex tail): poll_notifications now
    # filters ephemeral pytest/demo workstreams like the read path (spokesman.noise),
    # so a decision must live in a real workstream to surface in the digest.
    return f"realdec-{uuid4().hex[:8]}-live"


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
def test_decision_requested_renders_question_options_default(conn, ws) -> None:
    baseline = _max_seq(conn)
    d = request_decision(
        conn, workstream=ws, question="Which vendor should we pick?",
        options=["Vendor A", "Vendor B"], default_choice="Vendor A",
        sink=DbEventSink(conn),
    )
    batch = poll_notifications(conn, baseline)
    mine = [i for i in batch.items if i.decision_id == str(d.id)]
    assert len(mine) == 1
    item = mine[0]
    assert item.kind is NotifyKind.APPROVE
    assert item in batch.digest_items and item not in batch.alarms
    # Rendered from the decisions ROW (leak-free): question + options + default.
    assert "Which vendor should we pick?" in item.text
    assert "Vendor A" in item.text and "Vendor B" in item.text
    assert "Default if unanswered: Vendor A" in item.text
    assert f"decide {d.id}" in item.text

    # Batches into the ADR-0006 digest under the 🛑 "Needs approval" group.
    digest = compose_digest([PendingItem(NotifyKind.APPROVE, item.text)])
    assert str(d.id) in digest and "Needs approval" in digest


@pytestmark_live
def test_inbound_decide_answers_and_resumes_parked_task(conn, ws) -> None:
    # 1. A task is claimed (in_progress), then parked on an open-ended decision.
    task = enqueue_task(conn, workstream=ws, type="work.demo", payload={})
    claimed = claim_task(conn, worker_id="w-test", workstream=ws)
    assert claimed is not None and claimed.id == task.id
    d = request_decision(
        conn, workstream=ws, question="Tone for the launch post?",
        dependent_task_id=task.id, sink=DbEventSink(conn),  # free text
    )
    assert get_task(conn, task.id).status is TaskStatus.BLOCKED

    # 2. Inbound "decide <id> <multi-word answer>" through the bridge (dry-run reply).
    client = RecordingClient()
    settings = make_settings(Path("/tmp") / f"spk-{uuid4().hex}")
    msg = InboundMessage(
        message_id="wamid.X", sender="15550001111",
        text=f"decide {d.id} warm and concise", timestamp="1700000000",
    )
    result = handle_inbound_command(settings, client, db.connect, msg)
    assert result == {"command": "decide", "ok": True, "status": STATUS_ANSWERED}
    assert client.sent and str(d.id) in client.sent[0][0]

    # 3. The decision is answered in the DB (multi-word answer preserved)...
    seen = get_decision(conn, d.id)
    assert seen.status == STATUS_ANSWERED and seen.answer == "warm and concise"
    assert seen.answered_by.startswith("whatsapp:")
    # ...and the parked task was RESUMED (blocked → up_for_grabs).
    assert get_task(conn, task.id).status is TaskStatus.UP_FOR_GRABS


@pytestmark_live
def test_inbound_decide_usage_and_unknown_id(conn, ws) -> None:
    client = RecordingClient()
    settings = make_settings(Path("/tmp") / f"spk-{uuid4().hex}")

    # Missing answer → usage hint, no crash.
    msg = InboundMessage(message_id="m1", sender="15550001111",
                         text=f"decide {uuid4()}", timestamp="1")
    r1 = handle_inbound_command(settings, client, db.connect, msg)
    assert r1 == {"command": "decide", "ok": False, "error": "missing id/answer"}

    # Unknown/never-opened id → "not open".
    msg2 = InboundMessage(message_id="m2", sender="15550001111",
                          text=f"decide {uuid4()} anything", timestamp="1")
    r2 = handle_inbound_command(settings, client, db.connect, msg2)
    assert r2 == {"command": "decide", "ok": False, "error": "not open"}

    # Malformed id → "invalid id".
    msg3 = InboundMessage(message_id="m3", sender="15550001111",
                          text="decide not-a-uuid answer", timestamp="1")
    r3 = handle_inbound_command(settings, client, db.connect, msg3)
    assert r3 == {"command": "decide", "ok": False, "error": "invalid id"}
