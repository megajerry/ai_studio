"""Google (Gemini) provider adapter — thin, key-from-env, httpx.

Structural only for M3b: the key is read INSIDE this module from
``GOOGLE_API_KEY`` (ADR-0011, invariant 5) and is never logged or returned.
Tests never call :meth:`complete`; httpx is imported lazily. This adapter serves
both Gemini generation models and Google text embeddings (`text-embedding-*`).
"""

from __future__ import annotations

import os
from typing import Any

from ..registry import Usage
from .base import Completion, Message, messages_char_len

_API_KEY_ENV = "GOOGLE_API_KEY"
_DEFAULT_BASE = "https://generativelanguage.googleapis.com"


class GoogleProvider:
    """Calls the Gemini generateContent API. Only active when a key is present."""

    name = "google"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or os.environ.get("GOOGLE_BASE_URL", _DEFAULT_BASE)

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

        # Gemini uses "contents" with role "user"/"model" and "parts".
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": str(m.get("content", ""))}],
            }
            for m in messages
            if m.get("role") != "system"
        ]
        system_parts = [
            {"text": str(m.get("content", ""))}
            for m in messages
            if m.get("role") == "system"
        ]
        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}

        # The key travels as a header, never in the URL/query (avoids leaking it
        # into any request-line logging).
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        resp = httpx.post(
            f"{self._base_url}/v1beta/models/{model_id}:generateContent",
            json=body,
            headers=headers,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        meta = data.get("usageMetadata", {})
        usage = Usage(
            input_tokens=int(meta.get("promptTokenCount", 0)),
            output_tokens=int(meta.get("candidatesTokenCount", 0)),
            cached_tokens=int(meta.get("cachedContentTokenCount", 0)),
        )
        return Completion(
            text=text, usage=usage, model_id=model_id, provider=self.name
        )
