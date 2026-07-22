"""Search gateway tests (architecture §9).

Pure-logic tests (query_hash normalization, expiry predicate, dry-run
determinism, policy gate, provider swap, event hygiene) need NO database and
therefore ALWAYS run — never skip. The cache round-trip / cache-hit-no-call /
migration-idempotent tests use a live Postgres and SKIP cleanly (never error,
never hang) when none is reachable; with DATABASE_URL set they MUST run and pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.enforce import MemoryEventSink
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.policy import PolicyConfig
from runtime.search import (
    DryRunSearchProvider,
    SearchCache,
    SearchConfig,
    SearchDenied,
    SearchResult,
    is_expired,
    normalize_query,
    query_hash,
    resolve_provider,
    search,
)
from runtime.search.gateway import (
    EVENT_SEARCH_CACHE_HIT,
    EVENT_SEARCH_CACHE_MISS,
    EVENT_SEARCH_DENIED,
    EVENT_SEARCH_PROVIDER_CALL,
)

# --- shared fixtures / spies -------------------------------------------------

ALLOW_POLICY = PolicyConfig(roles={"researcher": frozenset({Capability.NET_FETCH})})
DENY_POLICY = PolicyConfig(roles={"pm": frozenset()})
DRYRUN_CONFIG = SearchConfig(default_provider="dryrun", ttl_s=3600)


class SpyProvider:
    """Records every search call so tests can assert it was / wasn't invoked."""

    def __init__(self, name: str = "spy", results: list[SearchResult] | None = None):
        self.name = name
        self.calls: list[tuple[str, int]] = []
        self._results = results or [
            SearchResult(title="T", url="https://example.invalid/x", snippet="S", score=1.0)
        ]

    def available(self) -> bool:
        return True

    def search(self, query: str, k: int = 5) -> list[SearchResult]:
        self.calls.append((query, k))
        return list(self._results)


class SpyCache:
    """In-memory stand-in for SearchCache; records puts, serves a preset get."""

    def __init__(self):
        self.store: dict[tuple[str, str, int], list[SearchResult]] = {}
        self.puts: list[tuple] = []
        self.gets: list[tuple] = []

    def get(self, qhash, provider, k):
        self.gets.append((qhash, provider, k))
        return None  # always a miss for the pure tests

    def put(self, qhash, provider, query, k, results, ttl_s):
        self.puts.append((qhash, provider, query, k, results, ttl_s))
        self.store[(qhash, provider, k)] = results
        return None


# ===========================================================================
# Pure logic — no DB (always runs)
# ===========================================================================


def test_normalize_query_collapses_and_lowercases():
    assert normalize_query("  Hello   World ") == "hello world"
    assert normalize_query("HELLO\tworld\n") == "hello world"


def test_query_hash_is_stable_and_normalization_insensitive():
    a = query_hash("  Hello   World ", "dryrun", 5)
    b = query_hash("hello world", "dryrun", 5)
    assert a == b
    assert len(a) == 64  # sha256 hexdigest


def test_query_hash_varies_by_provider_and_k():
    base = query_hash("q", "dryrun", 5)
    assert query_hash("q", "tavily", 5) != base
    assert query_hash("q", "dryrun", 10) != base


def test_is_expired_predicate():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert is_expired(None, now) is False  # never expires
    assert is_expired(now - timedelta(seconds=1), now) is True  # past
    assert is_expired(now + timedelta(seconds=1), now) is False  # future
    # Naive stored timestamp is treated as UTC, never raises.
    assert is_expired(datetime(2025, 1, 1), now) is True


def test_dryrun_provider_is_deterministic():
    p = DryRunSearchProvider()
    a = p.search("the quick brown fox", k=5)
    b = p.search("the quick brown fox", k=5)
    assert a == b
    assert len(a) == 5
    # Scores strictly descending.
    scores = [r.score for r in a]
    assert scores == sorted(scores, reverse=True)


def test_dryrun_provider_varies_by_query():
    p = DryRunSearchProvider()
    assert p.search("alpha", k=3) != p.search("beta", k=3)


def test_resolve_provider_defaults_to_dryrun():
    assert resolve_provider("dryrun").name == "dryrun"
    # An unknown / keyless real provider falls back to dry-run (keyless default).
    assert resolve_provider("tavily").name == "dryrun"


def test_policy_gate_denies_role_without_net_fetch():
    """A role without net.fetch is denied: no provider call, no cache write."""
    spy = SpyProvider()
    cache = SpyCache()
    sink = MemoryEventSink()
    with pytest.raises(SearchDenied):
        search(
            None, "pm", "some query",
            policy=DENY_POLICY, config=DRYRUN_CONFIG,
            provider_impl=spy, cache=cache, sink=sink,
        )
    assert spy.calls == []          # provider never called
    assert cache.puts == []         # nothing cached
    assert cache.gets == []         # denial happens before cache lookup
    assert sink.types() == [EVENT_SEARCH_DENIED]
    # The denial event names the missing capability, not the query.
    payload = sink.events[0].payload
    assert "net.fetch" in payload["missing_capabilities"]
    assert "some query" not in str(payload)


def test_provider_swap_records_configured_provider():
    """Provider selection is data: the configured/named provider is what's used."""
    sink = MemoryEventSink()
    spy = SpyProvider(name="ignored")  # impl name differs from requested
    search(
        None, "researcher", "q1",
        provider="exa", policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
        provider_impl=spy, cache=SpyCache(), sink=sink,
    )
    call = [e for e in sink.events if e.type == EVENT_SEARCH_PROVIDER_CALL][0]
    assert call.payload["provider"] == "exa"  # requested provider is the cache/event key
    assert spy.calls == [("q1", 5)]


def test_events_carry_no_secrets_or_result_bodies():
    """Miss path events carry counts/latency/provider only — never bodies/query."""
    secret_query = "launch codes alpha-secret-7788"
    spy = SpyProvider(
        results=[
            SearchResult(
                title="TOPSECRETTITLE",
                url="https://example.invalid/topsecreturl",
                snippet="topsecretbody detail",
                score=0.9,
            )
        ]
    )
    sink = MemoryEventSink()
    out = search(
        None, "researcher", secret_query,
        policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
        provider_impl=spy, cache=SpyCache(), sink=sink,
    )
    assert len(out) == 1
    assert sink.types() == [EVENT_SEARCH_CACHE_MISS, EVENT_SEARCH_PROVIDER_CALL]
    blob = str([e.payload for e in sink.events])
    for leak in (
        "launch codes", "alpha-secret-7788",
        "TOPSECRETTITLE", "topsecreturl", "topsecretbody",
    ):
        assert leak not in blob, f"leaked {leak!r} into events"
    # But the useful non-sensitive fields are present.
    call = [e for e in sink.events if e.type == EVENT_SEARCH_PROVIDER_CALL][0]
    assert call.payload["count"] == 1
    assert "latency_ms" in call.payload
    assert call.payload["provider"] == "dryrun"
    assert len(call.payload["query_hash"]) == 64  # hash, not the query


# ===========================================================================
# DB round-trip — live Postgres (skips cleanly when absent)
# ===========================================================================

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0006 (and prior) are applied
    try:
        yield c
    finally:
        c.close()


@pytestmark_db
def test_cache_miss_then_hit_no_second_provider_call(conn):
    """Miss → provider + cache populated; a second search HITS and never calls the provider."""
    q = f"cache round trip {uuid4().hex}"
    spy = SpyProvider(
        results=[SearchResult(title="hit", url="https://example.invalid/h", snippet="s", score=1.0)]
    )
    sink1 = MemoryEventSink()

    # 1st call — miss → provider called once, cache written.
    out1 = search(conn, "researcher", q, policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
                  provider_impl=spy, sink=sink1)
    assert len(out1) == 1
    assert spy.calls == [(q, 5)]
    assert EVENT_SEARCH_CACHE_MISS in sink1.types()
    assert EVENT_SEARCH_PROVIDER_CALL in sink1.types()

    # Cache row exists.
    cached = SearchCache(conn).get(query_hash(q, "dryrun", 5), "dryrun", 5)
    assert cached is not None
    assert cached.results[0].title == "hit"

    # 2nd call — HIT: provider NOT called again, results returned from cache.
    sink2 = MemoryEventSink()
    out2 = search(conn, "researcher", q, policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
                  provider_impl=spy, sink=sink2)
    assert out2 == out1
    assert spy.calls == [(q, 5)]  # still exactly one call — the hit made none
    assert sink2.types() == [EVENT_SEARCH_CACHE_HIT]


@pytestmark_db
def test_expired_entry_is_a_miss_and_refetches(conn):
    """An entry past its TTL is treated as a miss (re-calls the provider)."""
    q = f"expiring {uuid4().hex}"
    spy = SpyProvider()
    # ttl_s=0 → expires immediately (now() + 0s <= now()).
    search(conn, "researcher", q, ttl_s=0, policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
           provider_impl=spy, sink=MemoryEventSink())
    assert len(spy.calls) == 1
    # Second search must miss (expired) and call the provider again.
    sink = MemoryEventSink()
    search(conn, "researcher", q, ttl_s=0, policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
           provider_impl=spy, sink=sink)
    assert len(spy.calls) == 2
    assert EVENT_SEARCH_CACHE_MISS in sink.types()


@pytestmark_db
def test_denied_search_writes_no_cache_row(conn):
    """The policy deny path writes no cache row even with a live DB."""
    q = f"denied {uuid4().hex}"
    with pytest.raises(SearchDenied):
        search(conn, "pm", q, policy=DENY_POLICY, config=DRYRUN_CONFIG,
               sink=MemoryEventSink())
    assert SearchCache(conn).get(query_hash(q, "dryrun", 5), "dryrun", 5) is None


@pytestmark_db
def test_dryrun_gateway_persists_events_to_log(conn):
    """End-to-end with the default DbEventSink: search.* events land in the log."""
    q = f"event log {uuid4().hex}"
    ws = f"searchtest-{uuid4().hex[:12]}"
    search(conn, "researcher", q, policy=ALLOW_POLICY, config=DRYRUN_CONFIG,
           workstream=ws)  # sink defaults to DbEventSink(conn)
    types = {e.type for e in read_events(conn, workstream=ws)}
    assert EVENT_SEARCH_CACHE_MISS in types
    assert EVENT_SEARCH_PROVIDER_CALL in types


@pytestmark_db
def test_migration_idempotent(conn):
    """Re-running migrate applies nothing new (0006 already applied)."""
    assert migrate(conn) == []
