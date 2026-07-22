-- 0006 — search cache: every search is cached (architecture §9, ADR-0005).
--
-- The Search gateway (runtime/search/gateway.py) enforces
--   Request → Policy → [providers] → Cache → Memory
-- and "all searches cached": a cache HIT within TTL returns the stored results
-- and makes NO provider call. This table is that cache.
--
-- Key = (query_hash, provider, k): query_hash is a stable sha256 of the
-- normalized query + provider + k (runtime/search/cache.py::query_hash), and
-- provider/k are kept as their own columns so the same query cached under a
-- different provider (a config swap) or a different k is a distinct entry.
--
-- `results` is the provider-neutral result list as JSONB. `expires_at` is the
-- TTL horizon (NULL = never expires); the gateway applies the pure is_expired()
-- predicate to it so the TTL decision stays unit-testable.
--
-- Forward-only and idempotent (like 0004/0005): CREATE ... IF NOT EXISTS is a
-- no-op on re-run, and the migration runner skips already-applied files anyway.

CREATE TABLE IF NOT EXISTS search_cache (
    query_hash text        NOT NULL,           -- sha256(normalized query + provider + k)
    provider   text        NOT NULL,           -- requested provider (dryrun|tavily|exa|brave)
    query      text        NOT NULL,           -- original query (local cache only; never logged)
    k          int         NOT NULL,           -- number of results requested
    results    jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- provider-neutral SearchResult[]
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,                     -- TTL horizon; NULL = never expires
    PRIMARY KEY (query_hash, provider, k)
);

-- Supports TTL sweeps / expiry scans without a full table scan.
CREATE INDEX IF NOT EXISTS search_cache_expires_at_idx ON search_cache (expires_at);
