"""Stakeholder-communication classification and routing (ADR-0006).

Every studio output is one of three kinds:

- ``alarm``  🚨 interrupt      -> send to WhatsApp immediately.
- ``approve`` 🛑 blocks         -> batched into the pending digest.
- ``inform`` 📣 non-blocking    -> batched into the pending digest.

Only genuine alarms interrupt; approvals and informational items accumulate and
are pushed together via a periodic digest flush.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from .whatsapp import WhatsAppClient

Kind = Literal["approve", "inform", "alarm"]


class NotifyKind(str, Enum):
    APPROVE = "approve"
    INFORM = "inform"
    ALARM = "alarm"


_EMOJI = {
    NotifyKind.APPROVE: "\U0001F6D1",  # 🛑
    NotifyKind.INFORM: "\U0001F4E3",  # 📣
    NotifyKind.ALARM: "\U0001F6A8",  # 🚨
}


@dataclass
class PendingItem:
    kind: NotifyKind
    text: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _coerce_kind(kind: Kind | NotifyKind | str) -> NotifyKind:
    if isinstance(kind, NotifyKind):
        return kind
    try:
        return NotifyKind(str(kind).strip().lower())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(
            f"unknown notify kind {kind!r}; expected one of "
            f"{[k.value for k in NotifyKind]}"
        ) from exc


def compose_digest(items: list[PendingItem]) -> str:
    """Render a batched digest of pending approve/inform items for WhatsApp."""
    if not items:
        return "No pending updates."
    approvals = [i for i in items if i.kind is NotifyKind.APPROVE]
    informs = [i for i in items if i.kind is NotifyKind.INFORM]

    lines: list[str] = ["*AI Studio digest*"]
    if approvals:
        lines.append(f"\n{_EMOJI[NotifyKind.APPROVE]} Needs approval ({len(approvals)}):")
        lines.extend(f"  • {i.text}" for i in approvals)
    if informs:
        lines.append(f"\n{_EMOJI[NotifyKind.INFORM]} FYI ({len(informs)}):")
        lines.extend(f"  • {i.text}" for i in informs)
    return "\n".join(lines)


class Notifier:
    """Routes notifications: alarms go now, approve/inform are batched.

    The pending digest is kept in memory; a periodic flush (or the
    ``POST /digest/flush`` endpoint) sends it as a single message.
    """

    def __init__(self, client: WhatsAppClient) -> None:
        self._client = client
        self._pending: list[PendingItem] = []
        self._lock = threading.Lock()

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def notify(self, kind: Kind | NotifyKind | str, text: str) -> dict:
        """Classify and route a single notification.

        Returns a result dict: alarms include the send result; batched items
        report the new pending count.
        """
        resolved = _coerce_kind(kind)
        text = text.strip()
        if not text:
            raise ValueError("notification text must not be empty")

        if resolved is NotifyKind.ALARM:
            body = f"{_EMOJI[NotifyKind.ALARM]} {text}"
            result = self._client.send_text(body)
            return {"routed": "alarm", "sent": True, "send_result": result}

        with self._lock:
            self._pending.append(PendingItem(kind=resolved, text=text))
            count = len(self._pending)
        return {"routed": "digest", "sent": False, "pending_count": count}

    def flush(self) -> dict:
        """Send the pending digest (if any) and clear it."""
        with self._lock:
            items = self._pending
            self._pending = []

        if not items:
            return {"flushed": 0, "sent": False}

        body = compose_digest(items)
        result = self._client.send_text(body)
        return {"flushed": len(items), "sent": True, "send_result": result}
