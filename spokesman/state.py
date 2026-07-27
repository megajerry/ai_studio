"""State integration for the Spokesman (ADR-0007).

Inbound WhatsApp messages are appended (append-only JSONL) to
``<state>/inbox/`` so the host/remote session can act on them, and the text
``status`` returns a summary of ``<state>/status.md``.

Because ``state/`` is synced over git, this module must never persist personal
info in a committable form (ADR-0011): the sender's number is masked to its last
four digits before it is written, and the runtime inbound log is git-ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings

# Runtime inbound log filename (git-ignored — see .gitignore).
INBOUND_LOG_NAME = "whatsapp-inbound.jsonl"

# Cap the stored message body (hygiene; the log is short-lived working state).
MAX_STORED_TEXT = 4000


def mask_number(number: str | None) -> str:
    """Mask a phone number to its last 4 digits (never store the full value)."""
    if not number:
        return "(unknown)"
    tail = number[-4:]
    return f"****{tail}"


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str
    text: str
    timestamp: str


def iter_inbound_messages(payload: dict) -> list[InboundMessage]:
    """Extract text messages from a Meta WhatsApp webhook payload.

    Non-text messages and status callbacks (delivery receipts, etc.) are
    ignored. The structure is ``entry[].changes[].value.messages[]``.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                body = (msg.get("text", {}) or {}).get("body", "")
                messages.append(
                    InboundMessage(
                        message_id=msg.get("id", ""),
                        sender=msg.get("from", ""),
                        text=body,
                        timestamp=msg.get("timestamp", ""),
                    )
                )
    return messages


def record_inbound(
    settings: Settings,
    messages: list[InboundMessage],
    *,
    channel: str = "whatsapp",
) -> Path | None:
    """Append inbound messages to the git-ignored inbox JSONL (masked sender)."""
    if not messages:
        return None
    inbox_dir = settings.state_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    log_path = inbox_dir / INBOUND_LOG_NAME
    with log_path.open("a", encoding="utf-8") as fh:
        for msg in messages:
            record = {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "channel": channel,
                "message_id": msg.message_id,
                "from": mask_number(msg.sender),
                "text": msg.text[:MAX_STORED_TEXT],
                "wa_timestamp": msg.timestamp,
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return log_path


def status_summary(settings: Settings, *, max_chars: int = 1200) -> str:
    """Return a WhatsApp-friendly summary of ``state/status.md``."""
    status_path = settings.state_dir / "status.md"
    if not status_path.exists():
        return "No status.md found; the studio has not reported state yet."
    text = status_path.read_text(encoding="utf-8").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n… (truncated)"
