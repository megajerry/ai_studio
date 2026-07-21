"""Control-plane auth: /notify and /digest/flush require X-Spokesman-Token."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.config import Settings

from .conftest import API_TOKEN, make_settings

PROTECTED = [
    ("/notify", {"kind": "inform", "text": "hi"}),
    ("/digest/flush", None),
]


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.mark.parametrize("path,body", PROTECTED)
def test_rejects_without_token(settings: Settings, path: str, body: dict | None) -> None:
    resp = _client(settings).post(path, json=body)
    assert resp.status_code == 401


@pytest.mark.parametrize("path,body", PROTECTED)
def test_rejects_wrong_token(settings: Settings, path: str, body: dict | None) -> None:
    resp = _client(settings).post(
        path, json=body, headers={"X-Spokesman-Token": "nope"}
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("path,body", PROTECTED)
def test_accepts_correct_token(settings: Settings, path: str, body: dict | None) -> None:
    resp = _client(settings).post(
        path, json=body, headers={"X-Spokesman-Token": API_TOKEN}
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("path,body", PROTECTED)
def test_fails_closed_when_token_unset(
    state_dir: Path, path: str, body: dict | None
) -> None:
    # No SPOKESMAN_API_TOKEN configured -> endpoints unusable even with a header.
    settings = make_settings(state_dir, api_token="")
    resp = _client(settings).post(
        path, json=body, headers={"X-Spokesman-Token": "anything"}
    )
    assert resp.status_code == 401


def test_health_and_webhook_stay_public(settings: Settings) -> None:
    client = _client(settings)
    assert client.get("/health").status_code == 200
    # GET /webhook without a token still runs the handshake (returns 403 here
    # only because the verify token doesn't match), not the 401 auth gate.
    assert client.get("/webhook").status_code == 403
