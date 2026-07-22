# `runtime/search/` — the search gateway (architecture §9, ADR-0005)

Search from architecture §9. Agents **never search directly**; every search goes
through **one** function so it is always policy-gated, always cached, and always
observable, and so providers swap without touching callers:

```
Request → Policy → [Tavily | Exa | Brave | dry-run] → Cache → Memory
```

**Runs fully keyless.** With no API keys every path works via the deterministic
dry-run provider; real providers activate the moment their key is present.
Nothing here holds or logs a secret.

## Layout

| File | Purpose |
| --- | --- |
| `providers.py` | `SearchProvider` protocol, `SearchResult`, `DryRunSearchProvider` (keyless/deterministic), the `ADAPTERS` name→factory map + `get_adapter()` |
| `tavily.py` / `exa.py` / `brave.py` | thin real adapters (key from env, lazy `httpx`, `available()` = key present) — **structural, not exercised in tests** |
| `cache.py` | pure `normalize_query` / `query_hash` / `is_expired`; `SearchCache` (Postgres store) |
| `gateway.py` | `search()` — THE policy-gated, cached call site; `SearchDenied`; `resolve_provider()`; emits `search.*` events |
| `config.py` | `SearchConfig` + `load_search_config` (registry-as-data: env → local → example) |
| `../search.example.yaml` | committed default config (provider + ttl) |
| `../search.yaml` | real config (git-ignored) |
| `../migrations/0006_search_cache.sql` | the `search_cache` table (forward-only, idempotent) |

## The flow (`gateway.search`)

`search(conn, role, query, *, k=5, provider=None, sink=None, ttl_s=…, config=None,
policy=None, provider_impl=None, workstream="productivity", cache=None, …) ->
list[SearchResult]`:

1. **Policy first.** A search needs the `net.fetch` capability (🟢 green in
   `runtime/capabilities.py`). The role is checked via the policy engine
   (`runtime.policy.decide`, reusing M2). Anything other than ALLOW → emit
   `search.denied` and raise `SearchDenied` — **no provider call, no cache write.**
2. **Cache lookup.** Key = `query_hash(query, provider, k)` (stable sha256 over the
   normalized query + requested provider + k). A row present **and not expired**
   (`is_expired(expires_at)`) → emit `search.cache_hit` and return the stored
   results. **No provider call** — architecture §9's "all searches cached".
3. **Miss → provider.** `resolve_provider(name)` picks the backend (dry-run when
   `SEARCH_DRY_RUN` is set, the provider is `dryrun`, or the named adapter is
   unwired / keyless; else the real adapter). It runs (timed), results are stored
   via `SearchCache.put` (`expires_at = now() + ttl_s`, or NULL to never expire),
   and `search.cache_miss` + `search.provider_call` are emitted.
4. **Memory (optional, off by default).** When `config.remember_results` is true
   and a `conn` is present, a compact result summary (titles/urls only) is
   remembered into the **Knowledge** layer (`runtime.memory.remember`) for reuse.
   Best-effort — a memory failure never breaks a search.

Providers swap by editing `search.yaml` (`default_provider`) — the gateway and its
callers never change (ADR-0005, mirroring the model router). `provider_impl=` is a
test/advanced injection seam (e.g. a spy provider); the normal path resolves from
config.

## Events (counts / latency / provider only — never bodies)

Emitted through an injected `EventSink` (reused from `runtime.enforce`:
`DbEventSink` in production, `MemoryEventSink` in tests, `NullEventSink` to drop).
Payloads carry **no result bodies, no raw query, no API key** (invariants 5 & 6) —
the query appears only as its one-way `query_hash`:

| Event | Payload |
| --- | --- |
| `search.denied` | provider, query_hash, k, + policy decision (role, missing capabilities, reason) |
| `search.cache_hit` | provider, query_hash, k, count |
| `search.cache_miss` | provider, query_hash, k |
| `search.provider_call` | provider, served_by, dry_run, query_hash, k, count, latency_ms |

`served_by` / `dry_run` distinguish the requested (logical, cache-key) provider
from the backend that actually ran (dry-run when keyless).

## Cache schema (`migrations/0006_search_cache.sql`)

`search_cache` — `query_hash text`, `provider text`, `query text`, `k int`,
`results jsonb`, `created_at timestamptz`, `expires_at timestamptz` (NULL = never
expires). **Primary key `(query_hash, provider, k)`** (a config provider-swap or a
different `k` is a distinct entry); index on `expires_at` for TTL sweeps. The raw
`query` lives in the cache table (local source of truth) but is **never** written
to the event log. Forward-only + idempotent (`CREATE … IF NOT EXISTS`), like 0005.

## Config (registry-as-data — ADR-0005)

Resolved like the policy engine / model registry (env → local → committed example):

1. `$AI_STUDIO_SEARCH_FILE` (explicit path)
2. `runtime/search.yaml` (real, git-ignored)
3. `runtime/search.example.yaml` (committed default)

```yaml
default_provider: dryrun   # dryrun | tavily | exa | brave
ttl_s: 3600                # cache TTL seconds; null = never expire
remember_results: false    # opt-in Knowledge-layer memory of results
```

The example holds **no secrets** — provider names only. Keys (`TAVILY_API_KEY`,
`EXA_API_KEY`, `BRAVE_API_KEY`) are `.env` entries read *inside* each adapter
(ADR-0011); config never holds one.

## Adding a provider

1. Add `providers/<name>.py`-style adapter with `name`, `available()` (reads its
   own env key), and `search(query, k) -> list[SearchResult]`. Read the key inside
   the adapter; never log/return it. Import `httpx` lazily.
2. Register it in `providers.py`'s `ADAPTERS` (via a lazy factory).
3. Point `default_provider` (or a caller's `provider=`) at its `name`.

A provider whose key is absent is served in dry-run automatically — the keyless
default.

## Env

- `TAVILY_API_KEY` / `EXA_API_KEY` / `BRAVE_API_KEY` — provider keys, read only
  inside the adapters.
- `SEARCH_DRY_RUN=1` — force dry-run even when keys are present.
- `AI_STUDIO_SEARCH_FILE` — explicit config path override.

## Tests

```bash
export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
python -m runtime.migrate           # applies 0006 (idempotent)
python -m pytest runtime/tests/test_search.py -q
```

`test_search.py`: pure-logic tests (query_hash normalization, expiry predicate,
dry-run determinism, policy gate → deny + no call + no cache write, provider swap,
event hygiene — no secrets/bodies) run with **no DB** and always run; the cache
round-trip, cache-hit-makes-no-provider-call (asserted via a spy provider),
expired-entry-refetch, event-log, and migration-idempotent tests use a live
Postgres and **skip cleanly** when none is reachable.
