"""Brave Search adapter — thin, key-from-env, lazy httpx (STRUCTURAL, ADR-0005).

Structural only, like the model layer's real adapters: the key is read INSIDE
this module from ``BRAVE_API_KEY`` (ADR-0011, invariant 5) and is never logged,
returned, or attached to a result. Tests never call :meth:`search`; ``httpx`` is
imported lazily so the keyless path never depends on it.
"""

from __future__ import annotations

import os
from typing import Any

from .providers import SearchResult

_API_KEY_ENV = "BRAVE_API_KEY"
_DEFAULT_BASE = "https://api.search.brave.com"


class BraveSearchProvider:
    """Calls the Brave Web Search API. Only active when a key is present."""

    name = "brave"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or os.environ.get("BRAVE_BASE_URL", _DEFAULT_BASE)

    def available(self) -> bool:
        return bool(os.environ.get(_API_KEY_ENV))

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        api_key = os.environ.get(_API_KEY_ENV)
        if not api_key:
            raise RuntimeError(
                f"{self.name}: {_API_KEY_ENV} is not set (use dry-run instead)"
            )
        import httpx  # lazy: keyless/dry-run path never needs httpx

        params: dict[str, Any] = {"q": query, "count": max(1, k)}
        headers = {
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
        }
        resp = httpx.get(
            f"{self._base_url}/res/v1/web/search",
            params=params,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        hits = (data.get("web", {}) or {}).get("results", []) or []
        out: list[SearchResult] = []
        for i, r in enumerate(hits[: max(0, k)]):
            out.append(
                SearchResult(
                    title=r.get("title", "") or "",
                    url=r.get("url", "") or "",
                    snippet=r.get("description", "") or "",
                    # Brave does not return a numeric relevance score; derive a
                    # descending rank-based one so ordering is preserved.
                    score=round(1.0 - (i / max(1, k)), 4),
                )
            )
        return out
