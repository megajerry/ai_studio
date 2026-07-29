"""A2P SMS opt-in / consent-evidence page — must be public and carry the
Twilio 10DLC disclosures (STOP, HELP, program name, "do not sell")."""

from __future__ import annotations

from fastapi.testclient import TestClient

from spokesman.app import create_app
from spokesman.sms_optin import render_sms_opt_in

from .conftest import make_settings


def _client(tmp_path):
    settings = make_settings(tmp_path, api_token="secret-token")
    return TestClient(
        create_app(
            settings=settings,
            connect=lambda: (_ for _ in ()).throw(RuntimeError("no db")),
        )
    )


def test_render_sms_opt_in_contains_required_disclosures() -> None:
    html = render_sms_opt_in()
    assert html.lstrip().startswith("<!doctype html>")
    # Program identity + non-marketing purpose.
    assert "Jerry Studio / AI Studio Spokesman" in html
    # Frequency + rates disclosures (A2P).
    assert "Message frequency varies" in html
    assert "Message &amp; data rates may apply" in html
    # HELP / STOP keywords.
    assert "HELP" in html
    assert "STOP" in html
    # Consent is not sold/shared/transferred.
    assert "do not sell" in html
    # Links back to the sibling compliance pages.
    assert "/privacy" in html
    assert "/terms" in html


def test_sms_opt_in_page_is_public(tmp_path) -> None:
    client = _client(tmp_path)
    res = client.get("/sms-opt-in")
    assert res.status_code == 200, res.text
    assert res.text == render_sms_opt_in()
    # The disclosures survive over HTTP.
    assert "Jerry Studio / AI Studio Spokesman" in res.text
    assert "STOP" in res.text
    assert "HELP" in res.text
    assert "do not sell" in res.text
