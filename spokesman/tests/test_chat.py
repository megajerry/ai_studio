"""Web chat fallback — page embed + message API (must work when SMS is down)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.chat import extract_embedded_token, render_chat

from .conftest import API_TOKEN, make_settings


def _client(tmp_path: Path, *, api_token: str = API_TOKEN) -> TestClient:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    (state / "status.md").write_text("# Studio status\n\nAll systems nominal.\n", "utf-8")
    settings = make_settings(state, api_token=api_token)

    def boom_connect():
        raise RuntimeError("no db")

    return TestClient(create_app(settings=settings, connect=boom_connect))


def test_render_chat_embeds_json_token_not_html_entities() -> None:
    html = render_chat(channel="twilio_sms", dry_run=True, token="sec'ret&tok\"en")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "&#x27;" not in script
    assert "&quot;" not in script
    assert "&amp;" not in script
    embedded = extract_embedded_token(html)
    assert embedded == "sec'ret&tok\"en"


def test_render_chat_neutralizes_script_breakout_in_token() -> None:
    html = render_chat(channel="twilio_sms", dry_run=False, token="</script><img>")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    assert "</script>" not in script
    assert extract_embedded_token(html) == "</script><img>"
    assert "\\u003c/script\\u003e" in html


def test_chat_page_requires_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/chat").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/chat?token=wrong").status_code == 401


def test_chat_page_ok_with_query_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.get(f"/chat?token={API_TOKEN}")
    assert res.status_code == 200
    assert "AI Studio" in res.text
    assert extract_embedded_token(res.text) == API_TOKEN


def test_chat_message_requires_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post("/chat/message", json={"text": "status"})
    assert res.status_code == 401
    assert res.json()["detail"] == "invalid or missing token"


def test_chat_message_rejects_wrong_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        "/chat/message?token=nope",
        json={"text": "status"},
        headers={"X-Spokesman-Token": "nope"},
    )
    assert res.status_code == 401


def test_browser_flow_extract_token_then_post(tmp_path: Path) -> None:
    """Simulate the UI: load page → read embedded TOKEN → POST /chat/message."""
    client = _client(tmp_path)
    page = client.get(f"/chat?token={API_TOKEN}")
    assert page.status_code == 200
    token = extract_embedded_token(page.text)
    assert token == API_TOKEN

    # Query only (header may be stripped by some proxies)
    res = client.post(
        f"/chat/message?token={token}",
        json={"text": "status"},
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["replies"], body
    assert "status" in body["replies"][0].lower() or "task" in body["replies"][0].lower()

    # Header only
    res2 = client.post(
        "/chat/message",
        json={"text": "status"},
        headers={"X-Spokesman-Token": token},
    )
    assert res2.status_code == 200
    assert res2.json()["ok"] is True


def test_chat_message_unknown_command(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(
        f"/chat/message?token={API_TOKEN}",
        json={"text": "hello there"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert "Unknown command" in (body.get("note") or "")


def test_chat_message_validation_empty_text(tmp_path: Path) -> None:
    client = _client(tmp_path)
    res = client.post(f"/chat/message?token={API_TOKEN}", json={"text": ""})
    assert res.status_code == 422


def test_special_char_token_round_trip(tmp_path: Path) -> None:
    special = "a&b'c\"d</x>"
    client = _client(tmp_path, api_token=special)
    page = client.get("/chat", params={"token": special})
    assert page.status_code == 200
    embedded = extract_embedded_token(page.text)
    assert embedded == special
    res = client.post(
        "/chat/message",
        params={"token": embedded},
        json={"text": "status"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True
