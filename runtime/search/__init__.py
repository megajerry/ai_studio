"""Search gateway — policy-gated, always-cached, provider-agnostic (architecture §9).

    Request → Policy → [Tavily | Exa | Brave | dry-run] → Cache → Memory

Agents NEVER search directly; every search goes through
:func:`runtime.search.gateway.search`, which gates on ``net.fetch``, serves cache
hits without a provider call, stores misses, and emits events carrying only
counts/latency/provider — never result bodies, the raw query, or an API key.
Providers swap by config (``runtime/search.example.yaml``) without touching
callers. Runs fully keyless by default via :class:`DryRunSearchProvider`. See
runtime/search.md.
"""

from __future__ import annotations

from .cache import (
    CachedSearch,
    SearchCache,
    is_expired,
    normalize_query,
    query_hash,
)
from .config import (
    DEFAULT_TTL_S,
    SearchConfig,
    load_search_config,
    resolve_search_path,
)
from .gateway import (
    EVENT_SEARCH_CACHE_HIT,
    EVENT_SEARCH_CACHE_MISS,
    EVENT_SEARCH_DENIED,
    EVENT_SEARCH_PROVIDER_CALL,
    SearchDenied,
    resolve_provider,
    search,
)
from .providers import (
    ADAPTERS,
    DryRunSearchProvider,
    SearchProvider,
    SearchResult,
    get_adapter,
)

__all__ = [
    # providers
    "ADAPTERS",
    "DryRunSearchProvider",
    "SearchProvider",
    "SearchResult",
    "get_adapter",
    # config
    "DEFAULT_TTL_S",
    "SearchConfig",
    "load_search_config",
    "resolve_search_path",
    # cache
    "CachedSearch",
    "SearchCache",
    "is_expired",
    "normalize_query",
    "query_hash",
    # gateway
    "EVENT_SEARCH_CACHE_HIT",
    "EVENT_SEARCH_CACHE_MISS",
    "EVENT_SEARCH_DENIED",
    "EVENT_SEARCH_PROVIDER_CALL",
    "SearchDenied",
    "resolve_provider",
    "search",
]
