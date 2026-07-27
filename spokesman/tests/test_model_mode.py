"""Spokesman MODEL-mode transparency (ADR-0026).

A stubbed brain must never hide behind a LIVE channel banner: the model-mode
helper resolves which model converse would use and whether it is a real provider
or the keyless dry-run stub, WITHOUT a network call. These tests pin that helper,
the `/health` `model` block, and the chat header MODEL indicator across the
dry-run and keyed states.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.chat import extract_embedded_token, render_chat
from spokesman.context import spokesman_model_mode

from .conftest import API_TOKEN, make_settings

_KEY_ENV = "ANTHROPIC_API_KEY"
_DRY_ENV = "MODELS_DRY_RUN"


def _force_keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    """No provider key + not forced-dry-run → dry-run only because no key."""
    monkeypatch.delenv(_DRY_ENV, raising=False)
    monkeypatch.delenv(_KEY_ENV, raising=False)


def _force_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Anthropic key present + dry-run NOT forced → real provider."""
    monkeypatch.delenv(_DRY_ENV, raising=False)
    monkeypatch.setenv(_KEY_ENV, "sk-ant-test-not-a-real-key")


# --- helper ----------------------------------------------------------------

def test_helper_dry_run_when_models_dry_run_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_DRY_ENV, "1")
    monkeypatch.setenv(_KEY_ENV, "sk-ant-test-not-a-real-key")  # key present but forced off
    mode = spokesman_model_mode()
    assert mode["task"] == "converse"
    assert mode["dry_run"] is True
    assert mode["provider"] == "dryrun"


def test_helper_dry_run_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keyless(monkeypatch)
    mode = spokesman_model_mode()
    assert mode["dry_run"] is True
    assert mode["provider"] == "dryrun"
    # The resolved model id is still reported (non-secret) even in stub mode.
    assert mode["model"] == "claude-sonnet-5"


def test_helper_live_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keyed(monkeypatch)
    mode = spokesman_model_mode()
    assert mode["dry_run"] is False
    assert mode["provider"] == "anthropic"
    assert mode["model"] == "claude-sonnet-5"
    # NEVER leak the key value anywhere in the reported dict.
    assert "sk-ant-test-not-a-real-key" not in repr(mode)


def test_helper_safe_fallback_when_registry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import runtime.model.registry as registry_mod

    def boom(*_a, **_k):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(registry_mod, "load_registry", boom)
    mode = spokesman_model_mode()
    assert mode == {
        "task": "converse",
        "provider": "unknown",
        "model": None,
        "dry_run": True,
    }


# --- /health ---------------------------------------------------------------

def _client(tmp_path: Path) -> TestClient:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    (state / "status.md").write_text("# Studio status\n\nAll systems nominal.\n", "utf-8")
    settings = make_settings(state)

    def boom_connect():
        raise RuntimeError("no db")

    return TestClient(create_app(settings=settings, connect=boom_connect))


def test_health_model_block_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keyless(monkeypatch)
    body = _client(tmp_path).get("/health").json()
    assert body["status"] == "ok"
    model = body["model"]
    assert model["task"] == "converse"
    assert model["dry_run"] is True
    assert model["provider"] == "dryrun"


def test_health_model_block_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_keyed(monkeypatch)
    body = _client(tmp_path).get("/health").json()
    model = body["model"]
    assert model["dry_run"] is False
    assert model["provider"] == "anthropic"
    assert model["model"] == "claude-sonnet-5"


# --- chat banner -----------------------------------------------------------

def _banner_meta(page_html: str) -> str:
    """The header .meta line carrying the channel + model indicators."""
    return next(line for line in page_html.splitlines() if "web fallback" in line)


def test_render_chat_shows_model_live_indicator() -> None:
    page = render_chat(
        channel="twilio_sms",
        dry_run=False,
        token=API_TOKEN,
        model_mode={"task": "converse", "provider": "anthropic",
                    "model": "claude-sonnet-5", "dry_run": False},
    )
    meta = _banner_meta(page)
    assert "model: LIVE (claude-sonnet-5)" in meta
    assert extract_embedded_token(page) == API_TOKEN


def test_render_chat_shows_model_stub_indicator() -> None:
    page = render_chat(
        channel="twilio_sms",
        dry_run=True,
        token=API_TOKEN,
        model_mode={"task": "converse", "provider": "dryrun",
                    "model": "claude-sonnet-5", "dry_run": True},
    )
    meta = _banner_meta(page)
    assert "model: STUB (dry-run" in meta
    assert "ANTHROPIC_API_KEY" in meta
    assert extract_embedded_token(page) == API_TOKEN


def test_render_chat_defaults_to_stub_without_model_mode() -> None:
    """Back-compat: omitting model_mode degrades to STUB, never crashes."""
    page = render_chat(channel="twilio_sms", dry_run=False, token=API_TOKEN)
    assert "model: STUB" in _banner_meta(page)
    assert extract_embedded_token(page) == API_TOKEN


def test_render_chat_model_indicator_is_escaped() -> None:
    """A hostile model id is HTML-escaped in the header (no raw injection)."""
    page = render_chat(
        channel="twilio_sms",
        dry_run=False,
        token=API_TOKEN,
        model_mode={"task": "converse", "provider": "anthropic",
                    "model": "<img src=x onerror=alert(1)>", "dry_run": False},
    )
    assert "<img src=x onerror=alert(1)>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert extract_embedded_token(page) == API_TOKEN
