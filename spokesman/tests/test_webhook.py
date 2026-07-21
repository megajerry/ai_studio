"""Webhook verification handshake + inbound handling (signature-gated)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.config import Settings
from spokesman.state import INBOUND_LOG_NAME

from .conftest import APP_SECRET, VERIFY_TOKEN


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_verify_handshake_echoes_challenge(settings: Settings) -> None:
    client = _client(settings)
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "42abc",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "42abc"


def test_verify_handshake_rejects_bad_token(settings: Settings) -> None:
    client = _client(settings)
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "42abc",
        },
    )
    assert resp.status_code == 403


def test_health(settings: Settings) -> None:
    resp = _client(settings).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True


def _inbound_payload(text: str, sender: str = "15559998888") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.TEST",
                                    "from": sender,
                                    "type": "text",
                                    "timestamp": "1700000000",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def test_inbound_rejected_without_signature(settings: Settings) -> None:
    body = json.dumps(_inbound_payload("hi")).encode()
    resp = _client(settings).post("/webhook", content=body)
    assert resp.status_code == 403


def test_inbound_appends_to_inbox_and_masks_number(
    settings: Settings, state_dir: Path
) -> None:
    body = json.dumps(_inbound_payload("hello there", sender="15559998888")).encode()
    resp = _client(settings).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["received"] == 1

    log = state_dir / "inbox" / INBOUND_LOG_NAME
    assert log.exists()
    record = json.loads(log.read_text().strip())
    assert record["text"] == "hello there"
    # Full number is never persisted — only the masked tail.
    assert record["from"] == "****8888"
    assert "15559998888" not in log.read_text()


def test_status_keyword_triggers_reply(settings: Settings) -> None:
    body = json.dumps(_inbound_payload("Status")).encode()
    resp = _client(settings).post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert resp.status_code == 200
    assert resp.json()["replies"] == 1
