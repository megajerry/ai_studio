"""Twilio Programmable SMS client + webhook signature verification.

Secrets come from :class:`~spokesman.config.Settings`. Dry-run logs outbound
sends without calling Twilio. Signature check follows Twilio's public docs:
``Base64(HMAC-SHA1(auth_token, url + sorted_form_params))``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Mapping

import httpx

from .config import Settings
from .state import InboundMessage

logger = logging.getLogger("spokesman.twilio_sms")


def verify_twilio_signature(
    *,
    auth_token: str,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
) -> bool:
    """Validate ``X-Twilio-Signature`` for an inbound webhook POST."""
    if not auth_token or not signature or not url:
        return False
    # Twilio: concatenate the full URL with sorted POST params as key+value pairs.
    pieces = [url]
    for key in sorted(params):
        pieces.append(key)
        pieces.append(params[key])
    digest = hmac.new(
        auth_token.encode("utf-8"),
        "".join(pieces).encode("utf-8"),
        hashlib.sha1,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature.strip())


def iter_twilio_inbound(form: Mapping[str, str]) -> list[InboundMessage]:
    """Extract a text inbound from a Twilio Messaging webhook form body."""
    body = (form.get("Body") or "").strip()
    if not body:
        return []
    sender = (form.get("From") or "").strip()
    # Normalize +E.164-ish; Twilio usually sends +1...
    if sender.startswith("+"):
        sender_digits = sender[1:]
    else:
        sender_digits = "".join(ch for ch in sender if ch.isdigit())
    return [
        InboundMessage(
            message_id=form.get("MessageSid") or form.get("SmsMessageSid") or "",
            sender=sender_digits,
            text=body,
            timestamp=form.get("DateCreated") or "",
        )
    ]


class TwilioSMSClient:
    """Send SMS via Twilio REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        settings = self._settings
        recipient = to or settings.stakeholder_number
        if recipient and not recipient.startswith("+"):
            recipient = f"+{recipient}"

        if settings.dry_run:
            logger.info("[dry-run] Twilio SMS -> %s: %s", recipient or "(unset)", text)
            return {"dry_run": True, "channel": "twilio_sms", "to": recipient, "text": text}

        settings.require(
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
            "STAKEHOLDER_WHATSAPP_NUMBER",
        )
        from_number = settings.twilio_from_number
        if not from_number.startswith("+"):
            from_number = f"+{from_number}"

        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        resp = httpx.post(
            url,
            data={"From": from_number, "To": recipient, "Body": text},
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(
            "Sent Twilio SMS to %s sid=%s status=%s",
            recipient, data.get("sid"), data.get("status"),
        )
        return {
            "dry_run": False,
            "channel": "twilio_sms",
            "to": recipient,
            "response": {
                "sid": data.get("sid"),
                "status": data.get("status"),
                "error_code": data.get("error_code"),
            },
        }
