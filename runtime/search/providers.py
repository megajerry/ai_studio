"""Search providers — the provider-agnostic boundary (architecture §9, ADR-0005).

A :class:`SearchProvider` turns ``(query, k)`` into a list of :class:`SearchResult`.
Concrete adapters (tavily/exa/brave) read their API key from the environment
**inside themselves** and never return or log it (ADR-0011, invariant 5); the
:class:`DryRunSearchProvider` needs no key at all and is the keyless default.

This mirrors the model layer's provider pattern (``runtime/model/providers``):
providers are selected as *data* (by name, from config) and swap without touching
the gateway or its callers. Agents never call a provider directly — every search
goes through :func:`runtime.search.gateway.search`, the single policy-gated,
cached call site (architecture §9: "NEVER agent-direct").
"""

from __future__ import annotations

import hashlib
from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel


class SearchResult(BaseModel):
    """One search hit. Deliberately provider-neutral so results are uniform
    regardless of which backend produced them."""

    title: str
    url: str
    snippet: str = ""
    score: float = 0.0


@runtime_checkable
class SearchProvider(Protocol):
    """Anything that can answer a search query.

    ``name`` is the string used in config, cache keys, and events. ``available()``
    reports whether the provider can actually run (its key is present) so the
    gateway can fall back to dry-run when it cannot — exactly like the model
    layer's provider protocol.
    """

    name: str

    def available(self) -> bool:
        ...

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        ...


class DryRunSearchProvider:
    """Keyless, networkless, DETERMINISTIC provider (architecture §9 keyless default).

    Produces synthetic results derived purely from the query text, so the whole
    gateway path (policy → cache → provider → cache-store → events) runs and is
    reproducible with no API key and no network. Identical ``(query, k)`` always
    yields identical results, which is what makes the cache round-trip testable.
    """

    name = "dryrun"

    def available(self) -> bool:
        # Always available — that is the whole point of the keyless default.
        return True

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        results: list[SearchResult] = []
        n = max(0, k)
        for i in range(n):
            digest = hashlib.sha256(f"{query}:{i}".encode("utf-8")).hexdigest()
            results.append(
                SearchResult(
                    title=f"[dry-run] result {i + 1} for {query!r}",
                    # .invalid is a reserved, non-routable TLD (RFC 2606) — a
                    # synthetic URL can never accidentally hit a real host.
                    url=f"https://example.invalid/{digest[:12]}",
                    snippet=f"Synthetic snippet {digest[:8]} for query {query!r}.",
                    # Descending so the ordering is meaningful and deterministic.
                    score=round(1.0 - (i / n if n else 0.0), 4),
                )
            )
        return results


# --- Adapter registry (name -> factory) -------------------------------------
# Mirrors runtime/model/providers/__init__.py's ADAPTERS. A provider absent from
# this map (or whose key is unset) is served in dry-run — the keyless default.
# DryRunSearchProvider is intentionally NOT in the map: it is the fallback, not a
# routable/configurable backend. Factories import the adapter lazily so this
# module never imports httpx and the keyless path stays dependency-free.


def _tavily() -> SearchProvider:
    from .tavily import TavilySearchProvider

    return TavilySearchProvider()


def _exa() -> SearchProvider:
    from .exa import ExaSearchProvider

    return ExaSearchProvider()


def _brave() -> SearchProvider:
    from .brave import BraveSearchProvider

    return BraveSearchProvider()


ADAPTERS: dict[str, Callable[[], SearchProvider]] = {
    "tavily": _tavily,
    "exa": _exa,
    "brave": _brave,
}


def get_adapter(provider: str) -> SearchProvider | None:
    """Instantiate the adapter for ``provider``, or ``None`` if none is wired."""
    factory = ADAPTERS.get(provider)
    return factory() if factory is not None else None


__all__ = [
    "ADAPTERS",
    "DryRunSearchProvider",
    "SearchProvider",
    "SearchResult",
    "get_adapter",
]
