"""Meta WhatsApp Business Cloud API client + webhook signature verification.

Secrets are read from :class:`~spokesman.config.Settings`; this module is the
only place that talks to the Graph API on the studio's behalf (invariant: tools
call external services, agents never do). Supports a dry-run mode so the service
runs end-to-end with no live credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from .config import Settings

logger = logging.getLogger("spokesman.whatsapp")

_SIGNATURE_PREFIX = "sha256="


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Verify the ``X-Hub-Signature-256`` header against the raw request body.

    The signature is ``sha256=<hex HMAC-SHA256(raw_body, app_secret)>``. Uses a
    constant-time comparison. Returns ``False`` on any missing/malformed input.
    """
    if not header or not app_secret:
        return False
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    provided = header[len(_SIGNATURE_PREFIX):].strip()
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


class WhatsAppClient:
    """Thin client for sending WhatsApp text messages via the Graph API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def send_text(self, text: str, *, to: str | None = None) -> dict:
        """Send a plain-text WhatsApp message to the stakeholder.

        In dry-run mode the outbound call is logged and skipped, so no live
        credentials are needed. Returns a small result dict describing what
        happened (useful for tests and the event log).
        """
        settings = self._settings
        recipient = to or settings.stakeholder_number

        if settings.dry_run:
            logger.info("[dry-run] WhatsApp -> %s: %s", recipient or "(unset)", text)
            return {"dry_run": True, "to": recipient, "text": text}

        settings.require(
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "STAKEHOLDER_WHATSAPP_NUMBER",
        )

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(
            settings.messages_url, json=payload, headers=headers, timeout=30.0
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Sent WhatsApp message to %s (%s)", recipient, data)
        return {"dry_run": False, "to": recipient, "response": data}
