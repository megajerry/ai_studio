"""Search gateway — the ONLY way an agent searches (architecture §9, ADR-0005).

Architecture §9 flow, enforced here end to end:

    Request → Policy → [providers] → Cache → Memory      (NEVER agent-direct)

:func:`search` is the choke point:

1. **Policy first.** A search is a ``net.fetch`` capability (🟢 green). The role
   is checked through the policy engine; DENY → emit ``search.denied`` and raise
   :class:`SearchDenied` with NO provider call and NO cache write.
2. **Cache lookup.** Key = :func:`runtime.search.cache.query_hash` over the
   normalized query + provider + k. A hit within TTL emits ``search.cache_hit``
   and returns the stored results — NO provider call ("all searches cached").
3. **Miss → provider.** The configured provider (dry-run when keyless/forced)
   runs, results are stored in the cache, and ``search.cache_miss`` +
   ``search.provider_call`` are emitted (provider, count, latency — NEVER result
   bodies, the raw query, or an API key).
4. **Memory (optional, off by default).** When ``config.remember_results`` is set,
   a compact summary is remembered into the Knowledge layer for reuse.

Providers swap by config without touching this function or its callers (ADR-0005,
like the model router). Events flow through an injected :class:`EventSink` (reused
from :mod:`runtime.enforce`) so the whole path is testable with no database.
"""

from __future__ import annotations

import os
from time import perf_counter
from typing import Optional
from uuid import UUID

import psycopg

from ..capabilities import Capability
from ..enforce import DbEventSink, EventSink, NullEventSink
from ..event_types import (
    EVENT_SEARCH_CACHE_HIT,
    EVENT_SEARCH_CACHE_MISS,
    EVENT_SEARCH_DENIED,
    EVENT_SEARCH_PROVIDER_CALL,
)
from ..models import make_event
from ..policy import Decision, Effect, PolicyConfig, PolicyRequest, decide, load_policy
from .cache import SearchCache, query_hash
from .config import SearchConfig, load_search_config
from .providers import DryRunSearchProvider, SearchProvider, SearchResult, get_adapter

#: The ``search.*`` event types are imported from the canonical
#: :mod:`runtime.event_types` and re-exported from :mod:`runtime.search`.

#: Force dry-run even when a provider key is present (mirrors MODELS_DRY_RUN).
_DRY_RUN_ENV = "SEARCH_DRY_RUN"

_TOOL = "search"

# Sentinel so a caller can pass ttl_s=None ("never expire") distinct from
# "unspecified — use the config default".
_UNSET = object()


class SearchDenied(Exception):
    """Raised when the policy engine denies a role the ``net.fetch`` capability.

    Carries the :class:`~runtime.policy.Decision` so a caller can log/escalate.
    Nothing was fetched and nothing was cached.
    """

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__(decision.reason)


def _dry_run_forced() -> bool:
    return os.environ.get(_DRY_RUN_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_provider(name: str) -> SearchProvider:
    """Resolve the provider that will actually serve the search.

    Dry-run when forced (``SEARCH_DRY_RUN``), when ``name`` is ``dryrun``, or when
    the named adapter is not wired / has no key — the keyless default. Otherwise
    the named real adapter. Selection is data (the ``name`` comes from config or
    the caller), so providers swap without touching callers.
    """
    if _dry_run_forced() or name == "dryrun":
        return DryRunSearchProvider()
    adapter = get_adapter(name)
    if adapter is not None and adapter.available():
        return adapter
    return DryRunSearchProvider()


def search(
    conn: Optional[psycopg.Connection],
    role: str,
    query: str,
    *,
    k: int = 5,
    provider: Optional[str] = None,
    sink: Optional[EventSink] = None,
    ttl_s: object = _UNSET,
    config: Optional[SearchConfig] = None,
    policy: Optional[PolicyConfig] = None,
    provider_impl: Optional[SearchProvider] = None,
    workstream: str = "productivity",
    cache: Optional[SearchCache] = None,
    task_id: Optional[UUID] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> list[SearchResult]:
    """Run a policy-gated, cached search on behalf of ``role``.

    Returns the results (from cache on a hit, from the provider on a miss). Raises
    :class:`SearchDenied` if the role lacks ``net.fetch``. ``provider_impl`` is a
    test/advanced injection seam (e.g. a spy provider); the normal path resolves
    the provider from ``config``.
    """
    config = config or load_search_config()
    policy_cfg = policy or load_policy()
    if sink is None:
        sink = DbEventSink(conn) if conn is not None else NullEventSink()

    provider_name = provider or config.default_provider
    qhash = query_hash(query, provider_name, k)

    # --- 1. Policy first --------------------------------------------------
    request = PolicyRequest(
        role=role,
        tool=_TOOL,
        required_capabilities=frozenset({Capability.NET_FETCH}),
    )
    decision = decide(request, policy_cfg)
    if decision.effect is not Effect.ALLOW:
        sink.emit(
            make_event(
                workstream=workstream,
                type=EVENT_SEARCH_DENIED,
                task_id=task_id,
                trace_id=trace_id,
                span_id=span_id,
                payload={
                    "provider": provider_name,
                    "query_hash": qhash,  # a hash, never the raw query
                    "k": k,
                    **decision.to_payload(),
                },
            )
        )
        raise SearchDenied(decision)

    cache = cache or (SearchCache(conn) if conn is not None else None)

    # --- 2. Cache lookup --------------------------------------------------
    if cache is not None:
        cached = cache.get(qhash, provider_name, k)
        if cached is not None and not cached.expired():
            sink.emit(
                make_event(
                    workstream=workstream,
                    type=EVENT_SEARCH_CACHE_HIT,
                    task_id=task_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    payload={
                        "provider": provider_name,
                        "query_hash": qhash,
                        "k": k,
                        "count": len(cached.results),
                    },
                )
            )
            return cached.results

    # --- 3. Miss → provider ----------------------------------------------
    impl = provider_impl or resolve_provider(provider_name)
    served_by = getattr(impl, "name", provider_name)
    dry_run = served_by == "dryrun" and provider_name != "dryrun"

    t0 = perf_counter()
    results = impl.search(query, k)
    latency_ms = int((perf_counter() - t0) * 1000)

    ttl = config.ttl_s if ttl_s is _UNSET else ttl_s  # type: ignore[assignment]
    if cache is not None:
        cache.put(qhash, provider_name, query, k, results, ttl)

    sink.emit(
        make_event(
            workstream=workstream,
            type=EVENT_SEARCH_CACHE_MISS,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            payload={"provider": provider_name, "query_hash": qhash, "k": k},
        )
    )
    sink.emit(
        make_event(
            workstream=workstream,
            type=EVENT_SEARCH_PROVIDER_CALL,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            # Counts / latency / provider only — NEVER result bodies, the raw
            # query, or an API key (invariants 5 & 6).
            payload={
                "provider": provider_name,
                "served_by": served_by,
                "dry_run": dry_run,
                "query_hash": qhash,
                "k": k,
                "count": len(results),
                "latency_ms": latency_ms,
            },
        )
    )

    # --- 4. Memory (optional, off by default) -----------------------------
    if config.remember_results and conn is not None and results:
        _remember_results(conn, workstream, query, provider_name, qhash, results)

    return results


def _remember_results(
    conn: psycopg.Connection,
    workstream: str,
    query: str,
    provider_name: str,
    qhash: str,
    results: list[SearchResult],
) -> None:
    """Remember a compact result summary into the Knowledge layer for reuse.

    Best-effort and flag-gated (``config.remember_results``): a memory failure
    must never break a search, so this swallows exceptions. Stores titles/urls
    only (no snippets) under the workstream's Knowledge scope; the memory API
    emits its own ``memory.remembered`` event (ids/dims only).
    """
    try:
        from ..memory import MemoryLayer, Scope, remember

        summary = f"search:{query}\n" + "\n".join(
            f"- {r.title} ({r.url})" for r in results[:5]
        )
        remember(
            conn,
            Scope(workstream=workstream),
            MemoryLayer.KNOWLEDGE,
            summary,
            metadata={"kind": "search_result", "provider": provider_name, "query_hash": qhash},
        )
    except Exception:  # noqa: BLE001 - memory is a best-effort side channel
        pass


__all__ = [
    "EVENT_SEARCH_CACHE_HIT",
    "EVENT_SEARCH_CACHE_MISS",
    "EVENT_SEARCH_DENIED",
    "EVENT_SEARCH_PROVIDER_CALL",
    "SearchDenied",
    "resolve_provider",
    "search",
]
