"""Memory API — remember / recall, scope-enforced, event-emitting (architecture §7).

Two verbs an agent (via a tool) uses:

- :func:`remember` — embed ``text`` and insert a :class:`MemoryItem`; emit
  ``memory.remembered`` (layer/scope/id/dims only — NEVER the text).
- :func:`recall` — embed the query, brute-force cosine search WITHIN the scope +
  layer, return the top-k items; emit ``memory.recalled`` (count only).

Scope enforcement is the point (architecture §7 "agents read BY SCOPE, never
everything"). A recall is addressed to exactly one layer and cannot cross a
workstream/project/episode boundary:

- **episode**   → needs ``workstream`` + ``project`` + ``episode``; sees only that episode.
- **project**   → needs ``workstream`` + ``project``; sees only that project's ``project``-layer items.
- **knowledge** → needs ``workstream``; sees that workstream's knowledge (plus the
  global corpus ``'*'`` when ``include_global_knowledge=True``).
- **longterm**  → global.

The two helpers :func:`add_lesson` / :func:`recall_lessons` are the thin Knowledge-
layer interface the Retro loop uses to grow + inject the lessons corpus.

Events carry counts/ids only — no memory text, no embedding values (invariants 5 &
6): the log stays replayable without leaking remembered content.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg

from ..event_types import EVENT_MEMORY_RECALLED, EVENT_MEMORY_REMEMBERED
from ..events import append_event
from ..models import make_event
from .embed import embed
from .models import GLOBAL_WORKSTREAM, MemoryItem, MemoryLayer, Scope
from .vector import PostgresVectorStore, VectorStore

#: Memory event types (``memory.remembered`` / ``memory.recalled``) are imported
#: from the canonical :mod:`runtime.event_types`.


def _normalize_for_write(layer: MemoryLayer, scope: Scope) -> Scope:
    """Validate + canonicalize the scope for a WRITE at ``layer``.

    Ensures the required scope columns are present and blanks the ones a layer
    must not carry, so a stored row always matches exactly one recall shape. Raises
    ``ValueError`` on a scope that can't be addressed at this layer.
    """
    if not scope.workstream:
        raise ValueError("scope.workstream is required")

    if layer is MemoryLayer.EPISODE:
        if not scope.project or not scope.episode:
            raise ValueError("episode layer requires scope.project and scope.episode")
        return Scope(workstream=scope.workstream, project=scope.project, episode=scope.episode)
    if layer is MemoryLayer.PROJECT:
        if not scope.project:
            raise ValueError("project layer requires scope.project")
        return Scope(workstream=scope.workstream, project=scope.project, episode=None)
    if layer is MemoryLayer.KNOWLEDGE:
        return Scope(workstream=scope.workstream, project=None, episode=None)
    # LONGTERM: global; store under the given workstream for provenance only.
    return Scope(workstream=scope.workstream, project=None, episode=None)


def _validate_for_read(layer: MemoryLayer, scope: Scope) -> None:
    """Ensure a recall scope has the columns the layer's visibility rule needs."""
    if not scope.workstream:
        raise ValueError("scope.workstream is required")
    if layer is MemoryLayer.EPISODE and (not scope.project or not scope.episode):
        raise ValueError("episode recall requires scope.project and scope.episode")
    if layer is MemoryLayer.PROJECT and not scope.project:
        raise ValueError("project recall requires scope.project")


def remember(
    conn: psycopg.Connection,
    scope: Scope,
    layer: MemoryLayer,
    text: str,
    metadata: Optional[dict[str, Any]] = None,
    *,
    store: Optional[VectorStore] = None,
) -> MemoryItem:
    """Embed ``text`` and store it at ``layer`` within ``scope``; emit an event.

    Returns the persisted :class:`MemoryItem` (with id + created_at). The
    ``memory.remembered`` event carries only layer/scope/id/dims — never the text.
    """
    if not text or not text.strip():
        raise ValueError("cannot remember empty text")
    norm = _normalize_for_write(layer, scope)
    vector = embed(text)

    item = MemoryItem(
        layer=layer,
        workstream=norm.workstream,
        project=norm.project,
        episode=norm.episode,
        text=text,
        metadata=metadata or {},
        embedding=vector,
    )
    store = store or PostgresVectorStore(conn)
    stored = store.upsert(item)

    append_event(
        conn,
        make_event(
            workstream=norm.workstream,
            type=EVENT_MEMORY_REMEMBERED,
            payload={
                "layer": layer.value,
                "workstream": norm.workstream,
                "project": norm.project,
                "episode": norm.episode,
                "item_id": str(stored.id) if stored.id else None,
                "dims": len(vector),
                "has_metadata": bool(metadata),
            },
        ),
    )
    return stored


def recall(
    conn: psycopg.Connection,
    scope: Scope,
    layer: MemoryLayer,
    query: str,
    k: int = 5,
    *,
    include_global_knowledge: bool = False,
    min_score: Optional[float] = None,
    store: Optional[VectorStore] = None,
) -> list[MemoryItem]:
    """Return the top-``k`` items nearest to ``query`` WITHIN ``scope`` + ``layer``.

    Enforces the visibility rule (never crosses workstream/project/episode or
    layer). Emits ``memory.recalled`` with the count only.

    ``min_score`` (optional) is a cosine-similarity floor in ``[-1, 1]``: items
    scoring strictly below it are dropped, so a weak/irrelevant match is never
    returned. ``None`` (the default) applies no floor — behavior-preserving.
    A production caller injecting recalled context into a prompt should set a
    modest floor (recommended ~0.2 for the dry-run embedder) to keep only
    genuinely relevant items.
    """
    _validate_for_read(layer, scope)
    query_vec = embed(query)
    store = store or PostgresVectorStore(conn)
    scored = store.search(
        layer=layer,
        scope=scope,
        query_vec=query_vec,
        k=k,
        include_global_knowledge=include_global_knowledge,
    )
    if min_score is not None:
        scored = [(item, score) for item, score in scored if score >= min_score]
    items = [item for item, _score in scored]

    append_event(
        conn,
        make_event(
            workstream=scope.workstream,
            type=EVENT_MEMORY_RECALLED,
            payload={
                "layer": layer.value,
                "workstream": scope.workstream,
                "project": scope.project,
                "episode": scope.episode,
                "k": k,
                "count": len(items),
            },
        ),
    )
    return items


# --- Retro lessons corpus (Knowledge layer) ---------------------------------


def add_lesson(
    conn: psycopg.Connection,
    workstream: str,
    text: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    global_lesson: bool = False,
    store: Optional[VectorStore] = None,
) -> MemoryItem:
    """Record a Retro lesson in the Knowledge layer (architecture §7).

    ``global_lesson=True`` stores it under the global corpus (``'*'``) so every
    workstream can recall it; otherwise it is scoped to ``workstream``.
    """
    ws = GLOBAL_WORKSTREAM if global_lesson else workstream
    meta = dict(metadata or {})
    meta.setdefault("kind", "lesson")
    return remember(
        conn, Scope(workstream=ws), MemoryLayer.KNOWLEDGE, text, meta, store=store
    )


def recall_lessons(
    conn: psycopg.Connection,
    workstream: str,
    query: str,
    k: int = 5,
    *,
    include_global: bool = True,
    min_score: Optional[float] = None,
    store: Optional[VectorStore] = None,
) -> list[MemoryItem]:
    """Recall the lessons most relevant to ``query`` for injection into new work.

    Defaults to including the global lessons corpus (``'*'``) alongside the
    workstream's own — the corpus Retro grows and future work draws on.

    ``min_score`` (optional cosine floor) drops weakly-matching lessons; ``None``
    (default) is behavior-preserving. Injecting lessons into a live prompt should
    pass a modest floor (recommended ~0.2 for the dry-run embedder) so an
    irrelevant lesson is never applied.
    """
    return recall(
        conn,
        Scope(workstream=workstream),
        MemoryLayer.KNOWLEDGE,
        query,
        k,
        include_global_knowledge=include_global,
        min_score=min_score,
        store=store,
    )
