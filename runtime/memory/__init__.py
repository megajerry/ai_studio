"""Four-layer memory subsystem (architecture §7, ADR-0005).

Episode → Project → Knowledge → Long-term, backed by Postgres. Agents read BY
SCOPE via :func:`remember` / :func:`recall`; the Retro loop grows + injects the
Knowledge-layer lessons corpus via :func:`add_lesson` / :func:`recall_lessons`.
Embeddings run keyless by default (dry-run), and search is brute-force cosine over
the scope-filtered rows (no pgvector / Qdrant needed here). See runtime/memory.md.
"""

from __future__ import annotations

from .api import (
    EVENT_MEMORY_RECALLED,
    EVENT_MEMORY_REMEMBERED,
    add_lesson,
    recall,
    recall_lessons,
    remember,
)
from .embed import EMBED_DIM, dryrun_vector, embed, l2_normalize
from .models import (
    GLOBAL_WORKSTREAM,
    MemoryItem,
    MemoryLayer,
    Scope,
    in_scope,
    scope_where,
)
from .vector import (
    PostgresVectorStore,
    QdrantVectorStore,
    VectorStore,
    cosine,
)

__all__ = [
    # models + scope logic
    "GLOBAL_WORKSTREAM",
    "MemoryItem",
    "MemoryLayer",
    "Scope",
    "in_scope",
    "scope_where",
    # embeddings
    "EMBED_DIM",
    "dryrun_vector",
    "embed",
    "l2_normalize",
    # vector store
    "PostgresVectorStore",
    "QdrantVectorStore",
    "VectorStore",
    "cosine",
    # API
    "EVENT_MEMORY_RECALLED",
    "EVENT_MEMORY_REMEMBERED",
    "add_lesson",
    "recall",
    "recall_lessons",
    "remember",
]
