"""Spokesman conversation memory — the amnesia fix (ADR-0026; migration 0018).

BEFORE (the bug): ``handle_conversation`` rebuilt the prompt as
``[system, current_text]`` every turn, so turn 2 ("what's my name?") could not see
turn 1 ("my name is Jerry"). These tests pin the AFTER behavior: per-session
history is threaded into the model prompt and persisted, bounded by count + chars
(ADR-0013), isolated per session, body-free in the event log, and degrade-safe.
"""

from __future__ import annotations

import json
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spokesman import converse
from spokesman.app import create_app
from spokesman.state import InboundMessage

from .conftest import API_TOKEN, make_settings


class _Capture:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.sent.append(text)
        return {"ok": True, "to": to, "text": text}


def _settings(tmp_path: Path):
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    (state / "status.md").write_text("# ok\n", "utf-8")
    return make_settings(state)


# --- degrade-safe (no DB required) ------------------------------------------


def test_db_down_still_replies_no_exception(tmp_path: Path) -> None:
    """DB unavailable → no history, nothing persisted, but the human still gets a
    reply and the endpoint never raises (this surface exists for when things are
    down)."""
    settings = _settings(tmp_path)
    client = _Capture()

    def boom():
        raise RuntimeError("no db")

    msg = InboundMessage(message_id="1", sender="1555", text="hey there", timestamp="")
    outcome = converse.handle_conversation(
        settings, client, boom, msg, session_key="s-nodb"
    )
    assert outcome.intent == "converse"
    assert client.sent  # replied despite no DB


def test_dry_run_name_recall_from_threaded_history() -> None:
    """The keyless dry-run turn RECALLS a name stated in an earlier threaded turn
    (proves memory end-to-end with no provider key)."""
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "My name is Jerry"},
        {"role": "assistant", "content": "Nice to meet you."},
        {"role": "user", "content": "what's my name?"},
    ]
    turn = converse.build_dry_run_spokesman_turn("what's my name?", messages=history)
    assert turn["tool_calls"] == []
    assert "Jerry" in turn["reply"]

    # Without any prior turn stating it, it must NOT fabricate a name.
    blind = converse.build_dry_run_spokesman_turn(
        "what's my name?",
        messages=[{"role": "user", "content": "what's my name?"}],
    )
    assert "Jerry" not in blind["reply"]


# --- live DB ----------------------------------------------------------------

from runtime import db  # noqa: E402
from runtime.migrate import migrate  # noqa: E402
from spokesman.conversation import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    recent_turns,
    record_turn,
)

pytestmark_live = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def live_conn():
    c = db.connect()
    migrate(c)
    c.commit()
    try:
        yield c
    finally:
        c.close()


def _connect():
    return db.connect()


@pytestmark_live
def test_store_records_and_reads_back_oldest_first(live_conn) -> None:
    sk = "unit-" + uuid.uuid4().hex[:10]
    record_turn(live_conn, sk, "human", "first")
    record_turn(live_conn, sk, "spokesman", "second")
    record_turn(live_conn, sk, "human", "third")
    turns = recent_turns(live_conn, sk)
    assert [t.content for t in turns] == ["first", "second", "third"]
    assert [t.role for t in turns] == ["human", "spokesman", "human"]
    # role maps to model message roles.
    assert [t.as_message()["role"] for t in turns] == ["user", "assistant", "user"]


@pytestmark_live
def test_history_bounded_by_count(live_conn) -> None:
    sk = "bound-" + uuid.uuid4().hex[:10]
    for i in range(20):
        record_turn(live_conn, sk, "human", f"turn-{i}")
    turns = recent_turns(live_conn, sk, limit=5, max_chars=DEFAULT_MAX_CHARS)
    assert len(turns) == 5
    # newest 5 kept, oldest dropped, oldest-first order.
    assert [t.content for t in turns] == [f"turn-{i}" for i in range(15, 20)]


@pytestmark_live
def test_history_bounded_by_char_budget(live_conn) -> None:
    sk = "chars-" + uuid.uuid4().hex[:10]
    for i in range(6):
        record_turn(live_conn, sk, "human", "x" * 100)  # 100 chars each
    turns = recent_turns(live_conn, sk, limit=50, max_chars=250)
    # 250 budget → keeps the newest ~3 (300 > 250 stops after crossing); older dropped.
    assert 0 < len(turns) < 6
    assert sum(len(t.content) for t in turns) <= 250 + 100  # last kept may cross once


@pytestmark_live
def test_session_isolation(live_conn) -> None:
    a = "iso-a-" + uuid.uuid4().hex[:8]
    b = "iso-b-" + uuid.uuid4().hex[:8]
    record_turn(live_conn, a, "human", "secret-A")
    record_turn(live_conn, b, "human", "secret-B")
    assert [t.content for t in recent_turns(live_conn, a)] == ["secret-A"]
    assert [t.content for t in recent_turns(live_conn, b)] == ["secret-B"]


@pytestmark_live
def test_both_turns_persisted_per_exchange(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _Capture()
    sk = "exch-" + uuid.uuid4().hex[:10]
    converse.handle_conversation(
        settings,
        client,
        _connect,
        InboundMessage("1", "1555", "hello there", ""),
        session_key=sk,
    )
    conn = db.connect()
    try:
        rows = recent_turns(conn, sk)
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0].role == "human" and rows[0].content == "hello there"
    assert rows[1].role == "spokesman" and rows[1].content == client.sent[-1]


@pytestmark_live
def test_core_regression_turn2_sees_turn1_in_model_messages(
    tmp_path: Path, monkeypatch
) -> None:
    """CORE: BEFORE, turn 2's model messages were [system, 'what's my name?'] — turn
    1 was invisible (amnesia). AFTER, turn 2's messages CONTAIN turn 1's content."""
    settings = _settings(tmp_path)
    sk = "regress-" + uuid.uuid4().hex[:10]

    seen: list[list[str]] = []

    def fake_call_model(role, task_type, messages, **kw):
        seen.append([str(m.get("content") or "") for m in messages])
        return types.SimpleNamespace(
            text=json.dumps({"tool_calls": [], "reply": "ack"})
        )

    monkeypatch.setattr(converse, "call_model", fake_call_model)

    converse.handle_conversation(
        settings, _Capture(), _connect,
        InboundMessage("1", "1555", "my name is Jerry", ""), session_key=sk,
    )
    seen.clear()  # focus on turn 2's prompt
    converse.handle_conversation(
        settings, _Capture(), _connect,
        InboundMessage("2", "1555", "what's my name?", ""), session_key=sk,
    )
    turn2_messages = seen[0]
    # AFTER: turn 1's content is threaded into turn 2's prompt.
    assert any("my name is Jerry" in c for c in turn2_messages), turn2_messages
    # and the current question is still there.
    assert any("what's my name?" in c for c in turn2_messages)


@pytestmark_live
def test_event_log_stays_body_free(tmp_path: Path) -> None:
    """Dialogue text must NEVER appear in an emitted event payload (invariant 6):
    only char counts go on the wire; the body lives in spokesman_conversations."""
    settings = _settings(tmp_path)
    sentinel = "ZEBRA-" + uuid.uuid4().hex[:8]
    sk = "bodyfree-" + uuid.uuid4().hex[:10]

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(max(seq), 0) AS m FROM events")
            before = cur.fetchone()["m"]
        conn.commit()
    finally:
        conn.close()

    converse.handle_conversation(
        settings, _Capture(), _connect,
        InboundMessage("1", "1555", f"remember the code {sentinel}", ""),
        session_key=sk,
    )

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload::text AS p FROM events WHERE seq > %s", (before,)
            )
            payloads = [r["p"] for r in cur.fetchall()]
        conn.commit()
    finally:
        conn.close()

    assert payloads, "expected at least the EVENT_HUMAN_MESSAGE event"
    assert all(sentinel not in p for p in payloads), "dialogue body leaked into an event!"
    # but the body IS stored (DB-local) for memory.
    conn = db.connect()
    try:
        assert any(sentinel in t.content for t in recent_turns(conn, sk))
    finally:
        conn.close()


@pytestmark_live
def test_chat_message_threads_session_id_end_to_end(tmp_path: Path) -> None:
    """POST /chat/message with a session id, then a second POST with the SAME id
    sees the first turn (memory threaded through the web handler)."""
    settings = _settings(tmp_path)
    app = TestClient(create_app(settings=settings, connect=_connect))
    sk = "web-" + uuid.uuid4().hex[:10]

    r1 = app.post(
        f"/chat/message?token={API_TOKEN}",
        json={"text": "My name is Jerry", "session_key": sk},
    )
    assert r1.status_code == 200 and r1.json()["ok"] is True

    r2 = app.post(
        f"/chat/message?token={API_TOKEN}",
        json={"text": "what's my name?", "session_key": sk},
    )
    assert r2.status_code == 200
    replies = r2.json()["replies"]
    assert replies and "Jerry" in replies[-1], replies

    # A DIFFERENT session id must NOT see Jerry.
    r3 = app.post(
        f"/chat/message?token={API_TOKEN}",
        json={"text": "what's my name?", "session_key": "web-" + uuid.uuid4().hex[:8]},
    )
    assert "Jerry" not in r3.json()["replies"][-1]
