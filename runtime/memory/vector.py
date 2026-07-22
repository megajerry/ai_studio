"""Vector store — brute-force cosine over the scope-filtered rows (architecture §7).

Two things live here:

- :class:`VectorStore` — the protocol (``upsert`` + ``search``).
- :class:`PostgresVectorStore` — the DEFAULT, fully runnable backend. It fetches
  only the scope/layer-filtered candidate rows from ``memory_items`` via SQL
  (:func:`runtime.memory.models.scope_where`), then computes cosine similarity in
  Python and returns the top-k. No pgvector, no Qdrant, no extra deps.
- :class:`QdrantVectorStore` — a STRUCTURAL stub for the host (lazy import,
  documented, NOT used in tests). Qdrant is the phase-1 vector store (architecture
  §8) and slots in behind the same protocol without touching callers.

Scoping is enforced in the SQL WHERE (the candidate set) AND re-checked in Python
with :func:`in_scope` — defense in depth so a recall can never cross a
workstream/project/episode or layer boundary.
"""

from __future__ import annotations

import math
from typing import Any, Optional, Protocol

import psycopg
from psycopg.types.json import Jsonb

from .models import MemoryItem, MemoryLayer, Scope, in_scope, scope_where

_COLUMNS = "id, layer, workstream, project, episode, text, metadata, embedding, created_at"


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 if either is degenerate).

    Returns 0.0 rather than raising on a zero vector or a length mismatch, so a
    malformed/empty stored embedding simply never ranks — search stays robust.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class VectorStore(Protocol):
    """Backend the memory API talks to. Swap Postgres ↔ Qdrant behind this."""

    def upsert(self, item: MemoryItem) -> MemoryItem:
        ...

    def search(
        self,
        *,
        layer: MemoryLayer,
        scope: Scope,
        query_vec: list[float],
        k: int = 5,
        include_global_knowledge: bool = False,
    ) -> list[tuple[MemoryItem, float]]:
        ...


def _row_to_item(row: dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        id=row["id"],
        layer=MemoryLayer(row["layer"]),
        workstream=row["workstream"],
        project=row["project"],
        episode=row["episode"],
        text=row["text"],
        metadata=row["metadata"] or {},
        embedding=list(row["embedding"]) if row["embedding"] is not None else None,
        created_at=row["created_at"],
    )


class PostgresVectorStore:
    """Brute-force cosine over the scope/layer-filtered ``memory_items`` rows.

    The caller owns the connection (mirrors :mod:`runtime.events` / ``tasks``).
    Insert runs in its own transaction; search fetches only the in-scope
    candidates (SQL WHERE) then ranks them in Python.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def upsert(self, item: MemoryItem) -> MemoryItem:
        """Insert ``item`` and return it with the DB-assigned id + created_at."""
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO memory_items
                        (layer, workstream, project, episode, text, metadata, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING {_COLUMNS}
                    """,
                    (
                        item.layer.value,
                        item.workstream,
                        item.project,
                        item.episode,
                        item.text,
                        Jsonb(item.metadata),
                        item.embedding,
                    ),
                )
                row = cur.fetchone()
        return _row_to_item(row)

    def search(
        self,
        *,
        layer: MemoryLayer,
        scope: Scope,
        query_vec: list[float],
        k: int = 5,
        include_global_knowledge: bool = False,
    ) -> list[tuple[MemoryItem, float]]:
        """Return the top-``k`` (item, score) pairs within the scope, best first."""
        where, params = scope_where(
            layer, scope, include_global_knowledge=include_global_knowledge
        )
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM memory_items WHERE {where}",
                params,
            )
            rows = cur.fetchall()
        # Read-only: close the implicit transaction on a non-autocommit conn.
        if not self.conn.autocommit:
            self.conn.commit()

        scored: list[tuple[MemoryItem, float]] = []
        for row in rows:
            item = _row_to_item(row)
            # Defense in depth: the SQL already filters, but re-check the pure
            # predicate so scoping holds even if the two ever drift.
            if not in_scope(
                item, layer, scope, include_global_knowledge=include_global_knowledge
            ):
                continue
            scored.append((item, cosine(query_vec, item.embedding or [])))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[: max(0, k)]


class QdrantVectorStore:
    """STRUCTURAL stub for the host's Qdrant (architecture §8). NOT used in tests.

    Qdrant is the phase-1 vector memory. This adapter documents the swap-in: it
    would hold a collection per (workstream, layer) or filter by scope payload, and
    push cosine search into Qdrant instead of Python. The ``qdrant-client`` import
    is lazy so importing this module never requires the dependency; every method
    raises ``NotImplementedError`` until the host wires it up.
    """

    def __init__(self, *, url: Optional[str] = None, collection: str = "memory") -> None:
        self._url = url
        self._collection = collection

    def _client(self) -> Any:  # pragma: no cover - structural stub
        from qdrant_client import QdrantClient  # lazy; not a hard dependency here

        return QdrantClient(url=self._url)

    def upsert(self, item: MemoryItem) -> MemoryItem:  # pragma: no cover - structural stub
        raise NotImplementedError(
            "QdrantVectorStore is a structural stub; the default PostgresVectorStore "
            "is used everywhere in this repo. Wire this on the host."
        )

    def search(  # pragma: no cover - structural stub
        self,
        *,
        layer: MemoryLayer,
        scope: Scope,
        query_vec: list[float],
        k: int = 5,
        include_global_knowledge: bool = False,
    ) -> list[tuple[MemoryItem, float]]:
        raise NotImplementedError(
            "QdrantVectorStore is a structural stub; use PostgresVectorStore here."
        )
