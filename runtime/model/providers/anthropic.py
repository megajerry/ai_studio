"""Anthropic (Claude) provider adapter — thin, key-from-env, httpx.

Structural only for M3b: the key is read INSIDE this module from
``ANTHROPIC_API_KEY`` (ADR-0011, invariant 5) and is never logged, returned to a
caller, or placed on the :class:`Completion`. Tests never call :meth:`complete`
(the call wrapper forces dry-run without a key); httpx is imported lazily so the
keyless path never depends on it.
"""

from __future__ import annotations

import os
from typing import Any

from ..registry import Usage
from .base import Completion, Message, ProviderFallback

_API_KEY_ENV = "ANTHROPIC_API_KEY"
_DEFAULT_BASE = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"


def _api_model_id(model_id: str) -> str:
    """Map registry ids to Anthropic Messages API ids.

    Anthropic rejects dotted version suffixes (``claude-opus-4.8`` → 404 with a
    hint for ``claude-opus-4-8``). Hyphenate so a stale catalog still works.
    """
    return (model_id or "").replace(".", "-")


class AnthropicProvider:
    """Calls the Anthropic Messages API. Only active when a key is present."""

    name = "anthropic"

    def __init__(self, base_url: str | None = None) -> None:
        # Base URL is non-secret config; the key is fetched per-call from env so
        # it is never held as an attribute or serialized anywhere.
        self._base_url = base_url or os.environ.get(
            "ANTHROPIC_BASE_URL", _DEFAULT_BASE
        )

    def available(self) -> bool:
        return bool(os.environ.get(_API_KEY_ENV))

    def complete(
        self, model_id: str, messages: list[Message], *, max_tokens: int = 8192, **opts: Any
    ) -> Completion:
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"{self.name}: {_API_KEY_ENV} is not set (use dry-run instead)"
            )
        import httpx  # lazy: keyless/dry-run path never needs httpx

        api_model = _api_model_id(model_id)
        # Anthropic wants system prompts as a top-level field, not a message.
        system = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "system"
        )
        chat = [m for m in messages if m.get("role") != "system"]
        body: dict[str, Any] = {
            "model": api_model,
            "max_tokens": max_tokens,
            "messages": chat,
        }
        if system:
            body["system"] = system
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        resp = httpx.post(
            f"{self._base_url}/v1/messages", json=body, headers=headers, timeout=60.0
        )
        if resp.status_code == 404:
            # Unknown / retired model — walk the routed tier's fallback chain
            # rather than killing the whole worker pass.
            raise ProviderFallback(
                f"{self.name}: model {api_model!r} not found (HTTP 404)"
            )
        resp.raise_for_status()
        data = resp.json()

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        u = data.get("usage", {})
        usage = Usage(
            input_tokens=int(u.get("input_tokens", 0)),
            output_tokens=int(u.get("output_tokens", 0)),
            cached_tokens=int(u.get("cache_read_input_tokens", 0)),
        )
        return Completion(
            text=text, usage=usage, model_id=model_id, provider=self.name
        )
