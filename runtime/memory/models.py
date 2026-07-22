"""Typed models + pure scope logic for the four-layer memory (architecture §7).

Everything here is DB-free and unit-testable: the :class:`Scope`, the
:class:`MemoryLayer` enum, the :class:`MemoryItem` row model, and the two mirror
scope predicates (:func:`scope_where` for SQL candidate fetch, :func:`in_scope`
for pure-Python filtering). The data-access lives in :mod:`runtime.memory.vector`
and :mod:`runtime.memory.api`; the schema is ``runtime/migrations/0005_memory.sql``.

Scope & visibility rule (enforced on recall — see runtime/memory.md):

- **episode**   → visible only within ``(workstream, project, episode)``.
- **project**   → visible within ``(workstream, project)``.
- **knowledge** → visible within ``workstream`` (plus the global corpus
  ``workstream = '*'`` when ``include_global_knowledge`` is set).
- **longterm**  → global (visible everywhere).

A recall targets exactly ONE layer, so a narrower layer's items never surface in
a broader query, and no workstream/project can read another's memory.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

#: Sentinel workstream for globally-visible knowledge / long-term items. Chosen so
#: it can never collide with a real workstream slug.
GLOBAL_WORKSTREAM = "*"


class MemoryLayer(str, Enum):
    """The four memory layers, narrowest → broadest scope (architecture §7)."""

    EPISODE = "episode"
    PROJECT = "project"
    KNOWLEDGE = "knowledge"
    LONGTERM = "longterm"


class Scope(BaseModel):
    """Where an agent is reading/writing from — its addressable slice of memory.

    ``workstream`` is always required. ``project`` and ``episode`` narrow it; which
    ones must be present depends on the layer (validated in the API):

    - episode layer needs ``workstream`` + ``project`` + ``episode``
    - project layer needs ``workstream`` + ``project``
    - knowledge layer needs ``workstream`` (use ``GLOBAL_WORKSTREAM`` for global)
    - longterm layer needs only ``workstream`` (recall is global regardless)
    """

    workstream: str
    project: Optional[str] = None
    episode: Optional[str] = None


class MemoryItem(BaseModel):
    """One stored (or about-to-be-stored) memory row.

    ``id``/``created_at`` are ``None`` until the row is persisted (assigned by the
    DB). ``embedding`` is a plain float vector (see :mod:`runtime.memory.embed`).
    """

    layer: MemoryLayer
    workstream: str
    text: str
    id: Optional[UUID] = None
    project: Optional[str] = None
    episode: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[list[float]] = None
    created_at: Optional[datetime] = None


# --- Pure scope predicates --------------------------------------------------
# Two mirror implementations of the SAME visibility rule: `scope_where` builds the
# SQL WHERE for the candidate fetch; `in_scope` is the equivalent Python predicate
# (unit-tested with no DB, and used as a defensive re-check on fetched rows).


def scope_where(
    layer: MemoryLayer,
    scope: Scope,
    *,
    include_global_knowledge: bool = False,
) -> tuple[str, list[Any]]:
    """Build the ``WHERE`` clause + params selecting the candidates a recall may see.

    The clause always pins ``layer`` (so a broader query never returns a narrower
    layer's rows) and then applies the layer's scope columns per the visibility
    rule. Returns ``(sql_without_the_word_WHERE, params)``.
    """
    clauses = ["layer = %s"]
    params: list[Any] = [layer.value]

    if layer is MemoryLayer.EPISODE:
        clauses += ["workstream = %s", "project = %s", "episode = %s"]
        params += [scope.workstream, scope.project, scope.episode]
    elif layer is MemoryLayer.PROJECT:
        clauses += ["workstream = %s", "project = %s"]
        params += [scope.workstream, scope.project]
    elif layer is MemoryLayer.KNOWLEDGE:
        if include_global_knowledge:
            clauses.append("workstream IN (%s, %s)")
            params += [scope.workstream, GLOBAL_WORKSTREAM]
        else:
            clauses.append("workstream = %s")
            params.append(scope.workstream)
    elif layer is MemoryLayer.LONGTERM:
        pass  # global — no scope columns constrain a long-term recall

    return " AND ".join(clauses), params


def in_scope(
    item: MemoryItem,
    layer: MemoryLayer,
    scope: Scope,
    *,
    include_global_knowledge: bool = False,
) -> bool:
    """Pure mirror of :func:`scope_where`: is ``item`` visible to this recall?"""
    if item.layer is not layer:
        return False
    if layer is MemoryLayer.EPISODE:
        return (
            item.workstream == scope.workstream
            and item.project == scope.project
            and item.episode == scope.episode
        )
    if layer is MemoryLayer.PROJECT:
        return item.workstream == scope.workstream and item.project == scope.project
    if layer is MemoryLayer.KNOWLEDGE:
        if item.workstream == scope.workstream:
            return True
        return include_global_knowledge and item.workstream == GLOBAL_WORKSTREAM
    # LONGTERM: global.
    return True
