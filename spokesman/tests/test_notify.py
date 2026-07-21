"""Classification/notify routing (ADR-0006) + outbound send with mocked httpx."""

from __future__ import annotations

from pathlib import Path

import pytest

from spokesman.classify import Notifier, compose_digest
from spokesman.whatsapp import WhatsAppClient

from .conftest import STAKEHOLDER, make_settings


class RecordingClient:
    """Stand-in WhatsApp client that records sends instead of calling out."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        self.sent.append(text)
        return {"dry_run": True, "to": to, "text": text}


def test_alarm_sends_immediately() -> None:
    client = RecordingClient()
    notifier = Notifier(client)  # type: ignore[arg-type]
    result = notifier.notify("alarm", "security breach in progress")
    assert result["routed"] == "alarm"
    assert result["sent"] is True
    assert len(client.sent) == 1
    assert "security breach" in client.sent[0]
    assert notifier.pending_count == 0


@pytest.mark.parametrize("kind", ["approve", "inform"])
def test_approve_and_inform_are_batched(kind: str) -> None:
    client = RecordingClient()
    notifier = Notifier(client)  # type: ignore[arg-type]
    result = notifier.notify(kind, "milestone reached")
    assert result["routed"] == "digest"
    assert result["sent"] is False
    assert client.sent == []  # nothing sent yet
    assert notifier.pending_count == 1


def test_flush_sends_single_digest_and_clears() -> None:
    client = RecordingClient()
    notifier = Notifier(client)  # type: ignore[arg-type]
    notifier.notify("approve", "increase budget to $500/mo")
    notifier.notify("inform", "M0 verified on host")
    assert notifier.pending_count == 2

    result = notifier.flush()
    assert result["flushed"] == 2
    assert result["sent"] is True
    assert len(client.sent) == 1  # one batched message
    digest = client.sent[0]
    assert "increase budget" in digest
    assert "M0 verified" in digest

    # Digest cleared; a second flush is a no-op.
    assert notifier.pending_count == 0
    assert notifier.flush()["sent"] is False


def test_flush_empty_is_noop() -> None:
    client = RecordingClient()
    notifier = Notifier(client)  # type: ignore[arg-type]
    assert notifier.flush() == {"flushed": 0, "sent": False}


def test_unknown_kind_raises() -> None:
    notifier = Notifier(RecordingClient())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        notifier.notify("bogus", "text")


def test_empty_text_raises() -> None:
    notifier = Notifier(RecordingClient())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        notifier.notify("inform", "   ")


def test_compose_digest_groups_by_kind() -> None:
    from spokesman.classify import NotifyKind, PendingItem

    items = [
        PendingItem(NotifyKind.APPROVE, "approve me"),
        PendingItem(NotifyKind.INFORM, "fyi one"),
        PendingItem(NotifyKind.INFORM, "fyi two"),
    ]
    text = compose_digest(items)
    assert "Needs approval (1)" in text
    assert "FYI (2)" in text


def test_dry_run_client_does_not_call_out(state_dir: Path) -> None:
    settings = make_settings(state_dir, dry_run=True)
    client = WhatsAppClient(settings)
    result = client.send_text("hello")
    assert result["dry_run"] is True
    assert result["to"] == STAKEHOLDER


def test_live_send_uses_graph_api(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    settings = make_settings(state_dir, dry_run=False)
    calls: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"messages": [{"id": "wamid.OUT"}]}

    def fake_post(url: str, *, json: dict, headers: dict, timeout: float):  # noqa: A002
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return FakeResponse()

    import spokesman.whatsapp as wa

    monkeypatch.setattr(wa.httpx, "post", fake_post)

    client = WhatsAppClient(settings)
    result = client.send_text("live message")

    assert result["dry_run"] is False
    assert calls["url"] == settings.messages_url
    assert calls["json"]["to"] == STAKEHOLDER
    assert calls["json"]["text"]["body"] == "live message"
    assert calls["headers"]["Authorization"] == "Bearer test-token"
