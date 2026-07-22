"""Search cache — Postgres-backed, with pure key/expiry logic (architecture §9).

Architecture §9: "all searches cached". A cache HIT within TTL returns the stored
results and makes NO provider call. Two concerns live here:

- **Pure logic (no DB, unit-testable):** :func:`normalize_query` +
  :func:`query_hash` (a stable key for ``normalized query + provider + k``) and
  :func:`is_expired` (the TTL predicate).
- **Storage:** :class:`SearchCache`, a thin Postgres store over ``search_cache``
  (migration ``runtime/migrations/0006_search_cache.sql``). The caller owns the
  connection, mirroring :mod:`runtime.events` / :mod:`runtime.memory.vector`.

The cache key is the *requested* provider name (config default or the caller's
``provider=``), so the key is stable regardless of whether the real provider had
a key or was served in dry-run — swapping providers via config gives a distinct,
predictable cache namespace.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .providers import SearchResult


def normalize_query(query: str) -> str:
    """Canonicalize a query for hashing: trim, lower-case, collapse whitespace.

    So ``"  Hello   World "`` and ``"hello world"`` share one cache entry.
    """
    return " ".join(query.split()).lower()


def query_hash(query: str, provider: str, k: int) -> str:
    """Stable hash of the normalized ``query`` + ``provider`` + ``k``.

    Deterministic across processes (sha256 over a delimiter-joined tuple) so the
    same logical search always maps to the same cache key.
    """
    norm = normalize_query(query)
    material = f"{norm}\x00{provider}\x00{int(k)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_expired(expires_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True if a cache entry with ``expires_at`` is past its TTL at ``now``.

    ``expires_at is None`` means "never expires" (a TTL-less entry). Pure and
    DB-free so the TTL rule is unit-testable without Postgres.
    """
    if expires_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    # Treat a naive stored timestamp as UTC so comparison never raises.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= expires_at


class CachedSearch(BaseModel):
    """A row read back from ``search_cache``."""

    query_hash: str
    provider: str
    query: str
    k: int
    results: list[SearchResult] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def expired(self, now: Optional[datetime] = None) -> bool:
        return is_expired(self.expires_at, now)


def _results_to_json(results: list[SearchResult]) -> list[dict[str, Any]]:
    return [r.model_dump() for r in results]


def _json_to_results(raw: Any) -> list[SearchResult]:
    return [SearchResult.model_validate(r) for r in (raw or [])]


class SearchCache:
    """Postgres store over ``search_cache`` (caller owns the connection)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def get(self, qhash: str, provider: str, k: int) -> Optional[CachedSearch]:
        """Return the cached entry for ``(qhash, provider, k)`` or ``None``.

        Returns the row regardless of expiry — the gateway applies
        :func:`is_expired` so the TTL decision stays pure and testable. A read
        never mutates and never raises on a missing row.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT query_hash, provider, query, k, results, created_at, expires_at
                FROM search_cache
                WHERE query_hash = %s AND provider = %s AND k = %s
                """,
                (qhash, provider, k),
            )
            row = cur.fetchone()
        # Close the read's implicit transaction on a non-autocommit connection.
        if not self.conn.autocommit:
            self.conn.commit()
        if row is None:
            return None
        return CachedSearch(
            query_hash=row["query_hash"],
            provider=row["provider"],
            query=row["query"],
            k=row["k"],
            results=_json_to_results(row["results"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def put(
        self,
        qhash: str,
        provider: str,
        query: str,
        k: int,
        results: list[SearchResult],
        ttl_s: Optional[int],
    ) -> CachedSearch:
        """Upsert an entry; ``expires_at = now() + ttl_s`` (or NULL if ``ttl_s`` is None).

        ``created_at``/``expires_at`` use the DB clock so a single Postgres is the
        source of truth for TTL. Upsert (ON CONFLICT) so a re-search after expiry
        overwrites the stale entry cleanly.
        """
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO search_cache
                        (query_hash, provider, query, k, results, created_at, expires_at)
                    VALUES (
                        %s, %s, %s, %s, %s, now(),
                        CASE WHEN %s IS NULL THEN NULL
                             ELSE now() + make_interval(secs => %s) END
                    )
                    ON CONFLICT (query_hash, provider, k) DO UPDATE SET
                        query      = EXCLUDED.query,
                        results    = EXCLUDED.results,
                        created_at = EXCLUDED.created_at,
                        expires_at = EXCLUDED.expires_at
                    RETURNING query_hash, provider, query, k, results, created_at, expires_at
                    """,
                    (
                        qhash,
                        provider,
                        query,
                        k,
                        Jsonb(_results_to_json(results)),
                        ttl_s,
                        ttl_s,
                    ),
                )
                row = cur.fetchone()
        return CachedSearch(
            query_hash=row["query_hash"],
            provider=row["provider"],
            query=row["query"],
            k=row["k"],
            results=_json_to_results(row["results"]),
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )


__all__ = [
    "CachedSearch",
    "SearchCache",
    "is_expired",
    "normalize_query",
    "query_hash",
]
