"""Spokesman agent — model-first converse (ADR-0026)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from spokesman.app import create_app, handle_inbound_command
from spokesman.converse import (
    build_dry_run_spokesman_turn,
    handle_conversation,
)
from spokesman.handoff import format_handoff_relay
from spokesman.state import InboundMessage

from .conftest import API_TOKEN, make_settings


class _Capture:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.sent.append(text)
        return {"ok": True, "to": to, "text": text}


def test_dry_run_verbal_yes_calls_resolve_approval() -> None:
    turn = build_dry_run_spokesman_turn("yes, go ahead")
    assert turn["tool_calls"]
    assert turn["tool_calls"][0]["name"] == "resolve_approval"
    assert turn["tool_calls"][0]["args"]["decision"] == "approve"


def test_dry_run_greeting_is_conversational_not_status_dump() -> None:
    turn = build_dry_run_spokesman_turn("hello")
    assert turn["tool_calls"] == []
    assert turn["reply"]
    assert "_Re:" not in turn["reply"]
    assert "status.md" not in turn["reply"].lower()


def test_dry_run_casual_prompt_does_not_force_status_tool() -> None:
    turn = build_dry_run_spokesman_turn(
        "I'm thinking we should revisit the onboarding copy later"
    )
    assert turn["tool_calls"] == []
    assert "Got it" in (turn["reply"] or "")


def test_dry_run_requirement_uses_enqueue_tool() -> None:
    turn = build_dry_run_spokesman_turn("Please build a landing page for the demo")
    assert turn["tool_calls"]
    assert turn["tool_calls"][0]["name"] == "enqueue_goal"


def test_dry_run_status_question_uses_status_tool() -> None:
    turn = build_dry_run_spokesman_turn("how's the studio looking?")
    assert turn["tool_calls"]
    assert turn["tool_calls"][0]["name"] == "studio_status"


def test_format_handoff_relay() -> None:
    assert format_handoff_relay("pm", "hello") == "[PM] hello"


def test_keywords_still_fast_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "state")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "status.md").write_text("# ok\n", "utf-8")
    client = _Capture()

    def boom():
        raise RuntimeError("no db")

    msg = InboundMessage(
        message_id="1", sender="1555", text="status", timestamp=""
    )
    result = handle_inbound_command(settings, client, boom, msg)
    assert result == {"command": "status"}
    assert client.sent


def test_agent_greeting_conversation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "status.md").write_text("# Studio status\nNominal.\n", "utf-8")
    settings = make_settings(state)
    client = _Capture()

    def boom():
        raise RuntimeError("no db")

    msg = InboundMessage(
        message_id="2", sender="1555", text="hey", timestamp=""
    )
    outcome = handle_conversation(settings, client, boom, msg)
    assert outcome.intent == "converse"
    assert client.sent
    assert "Spokesman" in client.sent[0] or "Hey" in client.sent[0]
    assert "_Re:" not in client.sent[0]
    assert "Open tasks:" not in client.sent[0]


def test_web_chat_casual_message(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    (state / "status.md").write_text("# Studio status\nAll systems nominal.\n", "utf-8")
    settings = make_settings(state, api_token=API_TOKEN)

    def boom():
        raise RuntimeError("no db")

    client = TestClient(create_app(settings=settings, connect=boom))
    res = client.post(
        f"/chat/message?token={API_TOKEN}",
        json={"text": "hey, just checking in"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["replies"]
    assert body["result"]["command"] == "converse"
    assert "_Re:" not in body["replies"][0]


def test_converse_does_not_import_run_pm_tick() -> None:
    import inspect

    import spokesman.converse as mod

    src = inspect.getsource(mod)
    assert "run_pm_tick" not in src
    assert "PM_TICK_TYPE" in src
    assert "enqueue_task" in src
    assert "call_model" in src
    assert "studio_status" in src  # tool, not a forced reply path


def test_notify_via_handoff_blocked_without_active(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    settings = make_settings(state, api_token=API_TOKEN)

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = lambda s: s
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.execute.side_effect = RuntimeError("no table")
    conn.autocommit = True

    def connect():
        return conn

    client2 = TestClient(create_app(settings=settings, connect=connect))
    res = client2.post(
        "/notify",
        headers={"X-Spokesman-Token": API_TOKEN},
        json={
            "kind": "inform",
            "originating_identity": "role/pm",
            "via_handoff_role": "pm",
            "claims": [{"statement": "we should ship Tuesday", "is_judgment": True}],
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["blocked"] is True
    assert "handoff" in (body.get("reason") or "").lower()


# --- live DB ----------------------------------------------------------------

from runtime import db
from runtime.event_types import EVENT_SPOKESMAN_PREP_READY
from runtime.migrate import migrate
from runtime.models import Event
from runtime.scheduler import PM_TICK_TYPE
from spokesman.context import load_prep_cache, refresh_prep_cache
from spokesman.handoff import activate_handoff_for_approval, propose_handoff
from spokesman.runtime_bridge import classify_event

pytestmark_live = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def live_conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytestmark_live
def test_agent_enqueue_goal_via_tool(live_conn, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "status.md").write_text("# ok\n", "utf-8")
    settings = make_settings(state)
    client = _Capture()

    def connect():
        return db.connect()

    msg = InboundMessage(
        message_id="g1",
        sender="15550001111",
        text="Please implement the onboarding checklist",
        timestamp="",
    )
    outcome = handle_conversation(settings, client, connect, msg)
    assert outcome.intent == "converse"
    assert "enqueue_goal" in outcome.meta.get("tools", [])
    assert client.sent
    assert "Queued" in client.sent[0] or "PM" in client.sent[0]

    # Find the latest pm.tick with spokesman source
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT type, payload FROM tasks "
                "WHERE type = %s AND payload->>'source' = 'spokesman' "
                "ORDER BY created_at DESC NULLS LAST LIMIT 1",
                (PM_TICK_TYPE,),
            )
            row = cur.fetchone()
        if not conn.autocommit:
            conn.commit()
    finally:
        conn.close()
    assert row is not None
    payload = row["payload"] if isinstance(row, dict) else row[1]
    assert "onboarding" in payload["goal"].lower()


@pytestmark_live
def test_prep_cache_refresh_and_classify(live_conn) -> None:
    ctx = refresh_prep_cache(live_conn, notes=["prep note"])
    loaded = load_prep_cache(live_conn)
    assert loaded is not None
    assert loaded.status.open_tasks == ctx.status.open_tasks
    item = classify_event(
        Event(
            id=uuid4(),
            ts=datetime.now(timezone.utc),
            seq=1,
            task_id=None,
            workstream="productivity",
            type=EVENT_SPOKESMAN_PREP_READY,
            payload={"n_questions": 1},
        ),
        conn=live_conn,
    )
    assert item is not None
    assert "Follow-up" in item.text or "Prep" in item.text


@pytestmark_live
def test_handoff_propose_activate(live_conn) -> None:
    handoff, approval = propose_handoff(
        live_conn, role="pm", reason="Need deep PM discussion on scope"
    )
    assert handoff.status == "proposed"
    assert approval.id == handoff.approval_id
    activated = activate_handoff_for_approval(live_conn, approval.id)
    assert activated is not None
    assert activated.status == "active"
    assert activated.role == "pm"
