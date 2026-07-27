"""Messaging channel abstraction for the Spokesman (ADR-0006).

Outbound + inbound are provider-specific; classification / digest / commands stay
shared. Select the active channel with ``SPOKESMAN_CHANNEL``:

- ``whatsapp``     — Meta WhatsApp Cloud API (default)
- ``twilio_sms``   — Twilio Programmable SMS
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .config import Settings
from .twilio_sms import TwilioSMSClient
from .whatsapp import WhatsAppClient


@runtime_checkable
class MessagingClient(Protocol):
    """Minimal send surface used by :class:`~spokesman.classify.Notifier`."""

    def send_text(self, text: str, *, to: str | None = None) -> dict: ...


CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_TWILIO_SMS = "twilio_sms"
SUPPORTED_CHANNELS = frozenset({CHANNEL_WHATSAPP, CHANNEL_TWILIO_SMS})


def normalize_channel(raw: str | None) -> str:
    name = (raw or CHANNEL_WHATSAPP).strip().lower().replace("-", "_")
    if name in {"sms", "twilio"}:
        name = CHANNEL_TWILIO_SMS
    if name not in SUPPORTED_CHANNELS:
        raise ValueError(
            f"unknown SPOKESMAN_CHANNEL={raw!r}; expected one of "
            f"{sorted(SUPPORTED_CHANNELS)}"
        )
    return name


def build_messaging_client(settings: Settings) -> MessagingClient:
    """Construct the outbound client for the configured channel."""
    if settings.channel == CHANNEL_TWILIO_SMS:
        return TwilioSMSClient(settings)
    return WhatsAppClient(settings)
