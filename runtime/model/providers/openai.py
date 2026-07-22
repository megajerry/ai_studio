"""OpenAI provider adapter — thin, key-from-env, httpx.

Structural only for M3b: the key is read INSIDE this module from
``OPENAI_API_KEY`` (ADR-0011, invariant 5) and is never logged or returned.
Tests never call :meth:`complete`; httpx is imported lazily.
"""

from __future__ import annotations

import os
from typing import Any

from ..registry import Usage
from .base import Completion, Message

_API_KEY_ENV = "OPENAI_API_KEY"
_DEFAULT_BASE = "https://api.openai.com"


class OpenAIProvider:
    """Calls the OpenAI Chat Completions API. Only active when a key is present."""

    name = "openai"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL", _DEFAULT_BASE)

    def available(self) -> bool:
        return bool(os.environ.get(_API_KEY_ENV))

    def complete(
        self, model_id: str, messages: list[Message], **opts: Any
    ) -> Completion:
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"{self.name}: {_API_KEY_ENV} is not set (use dry-run instead)"
            )
        import httpx  # lazy: keyless/dry-run path never needs httpx

        body: dict[str, Any] = {"model": model_id, "messages": messages}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(
            f"{self._base_url}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()

        choices = data.get("choices", [])
        text = choices[0]["message"]["content"] if choices else ""
        u = data.get("usage", {})
        details = u.get("prompt_tokens_details", {}) or {}
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens", 0)),
            output_tokens=int(u.get("completion_tokens", 0)),
            cached_tokens=int(details.get("cached_tokens", 0)),
        )
        return Completion(
            text=text, usage=usage, model_id=model_id, provider=self.name
        )
