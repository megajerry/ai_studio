"""Record/replay scaffold for judge model I/O (VCR-style) — harness v2.

Real-model judge runs cost money and are non-deterministic, which makes them
useless in CI unless the I/O is **recorded once and replayed deterministically**.
This module is that seam. It is a *scaffold*: the dryrun judge needs no recording
(it is already deterministic), but the mechanism must exist NOW so that the moment
a real key lands, a real judge run can be recorded to a cassette and replayed in CI
with byte-for-byte reproducibility — no code change, only a mode flag.

A :class:`Cassette` is a JSON file of ``{request_key: recorded_completion}``
entries. The request key is a stable hash of ``(model_id, messages, opts)`` so the
same judge request always maps to the same recorded response.

Modes:

- ``OFF`` (default) — no recording, no replay; every call hits the live path
  (dryrun today). This is what the keyless CI uses.
- ``REPLAY`` — return the recorded completion for a known key; a cache MISS raises
  (a CI run must never silently fall back to a live/paid call).
- ``RECORD`` — call the live path, store the completion, and return it. Used once,
  off-CI, when a real key is present, to capture a golden transcript.

The judge (:mod:`evals.judge`) consults a cassette around its :func:`call_model`
invocation; with no cassette (or ``OFF``) it just calls through.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Cassette modes.
OFF = "off"
REPLAY = "replay"
RECORD = "record"


class CassetteMiss(KeyError):
    """Raised in REPLAY mode when a request has no recorded response.

    A miss must be loud: a reproducible CI run may not silently escalate to a live
    (paid, non-deterministic) model call.
    """


@dataclass
class RecordedCompletion:
    """The replayable subset of a provider ``Completion`` (text + usage + ids).

    Deliberately a plain, JSON-serializable record — the ``raw`` provider payload is
    intentionally dropped (it never needs to reach a replay and may carry bulk).
    """

    text: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    model_id: str
    provider: str

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "model_id": self.model_id,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecordedCompletion":
        return cls(
            text=d["text"],
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cached_tokens=int(d.get("cached_tokens", 0)),
            model_id=d.get("model_id", ""),
            provider=d.get("provider", ""),
        )

    @classmethod
    def from_completion(cls, completion: Any) -> "RecordedCompletion":
        """Capture the replayable subset of a live provider ``Completion``."""
        u = completion.usage
        return cls(
            text=completion.text,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cached_tokens=u.cached_tokens,
            model_id=completion.model_id,
            provider=completion.provider,
        )


def request_key(model_id: str, messages: list[dict], opts: Optional[dict] = None) -> str:
    """Stable content hash of a judge request → the cassette lookup key.

    Canonicalizes ``(model_id, messages, opts)`` with sorted keys so an identical
    request always hashes identically (order-independent, whitespace-stable).
    """
    payload = {
        "model_id": model_id,
        "messages": messages,
        "opts": opts or {},
    }
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Cassette:
    """A JSON-backed store of recorded judge completions (see module docstring)."""

    def __init__(self, path: Optional[str] = None, mode: str = OFF) -> None:
        if mode not in (OFF, REPLAY, RECORD):
            raise ValueError(f"unknown cassette mode {mode!r}")
        self.path = Path(path) if path else None
        self.mode = mode
        self._entries: dict[str, RecordedCompletion] = {}
        if self.path and self.path.exists():
            self.load()

    def load(self) -> None:
        """Load recorded entries from ``self.path`` (no-op if absent)."""
        if not self.path or not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8")) or {}
        self._entries = {
            k: RecordedCompletion.from_dict(v)
            for k, v in (data.get("entries") or {}).items()
        }

    def save(self) -> None:
        """Persist recorded entries to ``self.path`` (requires a path)."""
        if not self.path:
            raise ValueError("cannot save a cassette without a path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": 1, "entries": {k: v.to_dict() for k, v in self._entries.items()}}
        self.path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def get(self, key: str) -> Optional[RecordedCompletion]:
        """Return the recorded completion for ``key``, or ``None`` if absent."""
        return self._entries.get(key)

    def put(self, key: str, completion: RecordedCompletion) -> None:
        """Store a recorded completion under ``key``."""
        self._entries[key] = completion

    def replay(self, key: str) -> RecordedCompletion:
        """Return the recorded completion for ``key`` or raise :class:`CassetteMiss`."""
        rec = self._entries.get(key)
        if rec is None:
            raise CassetteMiss(f"no recorded completion for request {key[:12]}…")
        return rec


__all__ = [
    "OFF",
    "REPLAY",
    "RECORD",
    "CassetteMiss",
    "RecordedCompletion",
    "Cassette",
    "request_key",
]
