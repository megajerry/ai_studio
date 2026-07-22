"""Exa search adapter — thin, key-from-env, lazy httpx (STRUCTURAL, ADR-0005).

Structural only, like the model layer's real adapters: the key is read INSIDE
this module from ``EXA_API_KEY`` (ADR-0011, invariant 5) and is never logged,
returned, or attached to a result. Tests never call :meth:`search`; ``httpx`` is
imported lazily so the keyless path never depends on it.
"""

from __future__ import annotations

import os
from typing import Any

from .providers import SearchResult

_API_KEY_ENV = "EXA_API_KEY"
_DEFAULT_BASE = "https://api.exa.ai"


class ExaSearchProvider:
    """Calls the Exa Search API. Only active when a key is present."""

    name = "exa"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or os.environ.get("EXA_BASE_URL", _DEFAULT_BASE)

    def available(self) -> bool:
        return bool(os.environ.get(_API_KEY_ENV))

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"{self.name}: {_API_KEY_ENV} is not set (use dry-run instead)"
            )
        import httpx  # lazy: keyless/dry-run path never needs httpx

        body: dict[str, Any] = {
            "query": query,
            "numResults": max(1, k),
            "contents": {"text": True},
        }
        headers = {"x-api-key": api_key, "content-type": "application/json"}
        resp = httpx.post(
            f"{self._base_url}/search", json=body, headers=headers, timeout=30.0
        )
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for r in data.get("results", [])[: max(0, k)]:
            out.append(
                SearchResult(
                    title=r.get("title", "") or "",
                    url=r.get("url", "") or "",
                    snippet=(r.get("text", "") or "")[:500],
                    score=float(r.get("score", 0.0) or 0.0),
                )
            )
        return out
