"""Twilio SMS channel — signature + dry-run send (no network)."""

from __future__ import annotations

from spokesman.channel import CHANNEL_TWILIO_SMS, build_messaging_client
from spokesman.twilio_sms import (
    TwilioSMSClient,
    iter_twilio_inbound,
    verify_twilio_signature,
)

from .conftest import make_settings


def test_verify_twilio_signature_roundtrip() -> None:
    auth = "auth-token"
    url = "https://example.com/webhook"
    params = {"Body": "status", "From": "+18125550123", "To": "+12695556805"}
    # Compute expected the same way as the verifier.
    import base64
    import hashlib
    import hmac

    pieces = [url]
    for key in sorted(params):
        pieces.append(key)
        pieces.append(params[key])
    sig = base64.b64encode(
        hmac.new(auth.encode(), "".join(pieces).encode(), hashlib.sha1).digest()
    ).decode()
    assert verify_twilio_signature(
        auth_token=auth, url=url, params=params, signature=sig
    )
    assert not verify_twilio_signature(
        auth_token=auth, url=url, params=params, signature="nope"
    )


def test_iter_twilio_inbound_extracts_body() -> None:
    msgs = iter_twilio_inbound(
        {
            "Body": "approve abc",
            "From": "+18125550123",
            "MessageSid": "SMxxx",
        }
    )
    assert len(msgs) == 1
    assert msgs[0].text == "approve abc"
    assert msgs[0].sender == "18125550123"
    assert msgs[0].message_id == "SMxxx"


def test_twilio_dry_run_send(tmp_path) -> None:
    settings = make_settings(tmp_path, dry_run=True, channel=CHANNEL_TWILIO_SMS)
    client = TwilioSMSClient(settings)
    result = client.send_text("hello")
    assert result["dry_run"] is True
    assert result["channel"] == "twilio_sms"
    assert settings.stakeholder_number in result["to"]


def test_build_client_selects_twilio(tmp_path) -> None:
    settings = make_settings(tmp_path, channel=CHANNEL_TWILIO_SMS)
    client = build_messaging_client(settings)
    assert isinstance(client, TwilioSMSClient)
