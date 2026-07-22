"""Tavily search adapter — thin, key-from-env, lazy httpx (STRUCTURAL, ADR-0005).

Structural only, exactly like the model layer's real adapters: the key is read
INSIDE this module from ``TAVILY_API_KEY`` (ADR-0011, invariant 5) and is never
logged, returned to a caller, or placed on a :class:`SearchResult`. Tests never
call :meth:`search` (the gateway forces dry-run without a key); ``httpx`` is
imported lazily so the keyless path never depends on it.
"""

from __future__ import annotations

import os
from typing import Any

from .providers import SearchResult

_API_KEY_ENV = "TAVILY_API_KEY"
_DEFAULT_BASE = "https://api.tavily.com"


class TavilySearchProvider:
    """Calls the Tavily Search API. Only active when a key is present."""

    name = "tavily"

    def __init__(self, base_url: str | None = None) -> None:
        # Base URL is non-secret config; the key is fetched per-call from env so
        # it is never held as an attribute or serialized anywhere.
        self._base_url = base_url or os.environ.get("TAVILY_BASE_URL", _DEFAULT_BASE)

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
            "api_key": api_key,
            "query": query,
            "max_results": max(1, k),
        }
        resp = httpx.post(f"{self._base_url}/search", json=body, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        out: list[SearchResult] = []
        for r in data.get("results", [])[: max(0, k)]:
            out.append(
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", ""),
                    score=float(r.get("score", 0.0) or 0.0),
                )
            )
        return out
