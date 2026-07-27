"""The single guarded writer/reader for the FREE-FORM training-data store (ADR-0032).

Several event emit sites historically embedded FREE-FORM TEXT in ``events.payload``:
the PM's verbatim ``goal`` objective (and a free-text ``reason`` on pushback /
needs-clarification), and the MODEL-authored verifier ``verdict.reason`` on
``verify.passed`` / ``verify.failed`` / ``work.retry``. That text does not belong on
the append-only event log — which must stay BODY-FREE (ids/types/counts only,
invariant #6) — but the stakeholder wants it KEPT, not redacted, as self-improvement
TRAINING DATA. This module is where it is relocated: the full free-text lives in the
LOCAL DB ONLY (invariant #7, ``event_free_form`` in migration 0019), linked back to
its originating task/event, exactly like trajectory bodies (ADR-0020).

Discipline (mirrors :func:`runtime.trajectory.add_step` / :mod:`runtime.events`):

- **Single guarded writer.** ALL writes to ``event_free_form`` go through
  :func:`record_free_form` — no ad-hoc INSERTs elsewhere. Parameterized SQL only.
- **Observe-only, never load-bearing.** Relocating training data must NEVER break an
  emit. :func:`record_free_form` DEGRADES GRACEFULLY (ADR-0017): with no ``conn`` (the
  fake-queue unit paths), empty content, an unknown ``kind``, or on any write failure
  it logs + returns ``None`` — the caller's event still emits and its flow is
  unchanged. It does not raise.
- **Linkage.** A row records ``(event_type, workstream)`` always and, when known,
  ``task_id`` / ``trajectory_id`` — so the relocated text ties back to the body-free
  event that references the same task/type. ``event_seq`` is retained (nullable) for
  callers that can supply it; the ``EventSink`` write path does not surface it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel

log = logging.getLogger("runtime.free_form")

#: Closed vocabulary, mirrored by the migration 0019 CHECK constraint.
#:   goal      — a PM objective / restated-goal string
#:   reason    — a PM plan reason (pushback / needs_clarification)
#:   rationale — MODEL-authored verifier prose (verify.* / work.retry verdict.reason)
KINDS: frozenset[str] = frozenset({"goal", "reason", "rationale"})

_COLUMNS = (
    "id, event_seq, task_id, trajectory_id, event_type, workstream, "
    "kind, content, created_at"
)


class FreeForm(BaseModel):
    """One persisted free-form training-data row (relocated from an event payload)."""

    id: UUID
    event_seq: Optional[int] = None
    task_id: Optional[UUID] = None
    trajectory_id: Optional[UUID] = None
    event_type: str
    workstream: str
    kind: str
    content: str
    created_at: datetime


def _to_free_form(row: dict) -> FreeForm:
    return FreeForm.model_validate(dict(row))


def record_free_form(
    conn: Any,
    *,
    kind: str,
    content: Optional[str],
    event_type: str,
    workstream: str,
    task_id: Optional[UUID] = None,
    trajectory_id: Optional[UUID] = None,
    event_seq: Optional[int] = None,
) -> Optional[UUID]:
    """Relocate one free-form string into the local training-data store (ADR-0032).

    Returns the new row id, or ``None`` when nothing was stored. DEGRADE-SAFE and
    OBSERVE-ONLY: with ``conn is None`` (unit/fake-queue paths), blank ``content``, an
    unknown ``kind``, or on ANY write failure it logs + returns ``None`` — it never
    raises, so relocating training data can never break the caller's event emit
    (invariant safety / ADR-0017). Parameterized SQL only.
    """
    if conn is None:
        return None
    text = (content or "").strip()
    if not text:
        return None
    if kind not in KINDS:
        log.warning("free_form: unknown kind %r (allowed: %s); skipping",
                    kind, sorted(KINDS))
        return None
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_free_form
                        (event_seq, task_id, trajectory_id, event_type,
                         workstream, kind, content)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (event_seq, task_id, trajectory_id, event_type,
                     workstream, kind, text),
                )
                row = cur.fetchone()
        return row["id"] if row else None
    except Exception:  # pragma: no cover - defensive: relocation is never load-bearing
        log.warning("free_form: record failed for %s/%s; event emit continues",
                    event_type, kind, exc_info=True)
        return None


def read_free_form(
    conn: Any,
    *,
    kind: Optional[str] = None,
    task_id: Optional[UUID] = None,
    event_type: Optional[str] = None,
    workstream: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[FreeForm]:
    """Read relocated free-form rows for training-data retrieval (parameterized).

    Filters compose (AND); all are optional. Ordered by ``created_at`` then ``id``
    (stable). ``limit`` caps the result. Read-only; safe to call with a plain
    connection (commits the implicit read txn when not autocommit).
    """
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    if task_id is not None:
        clauses.append("task_id = %s")
        params.append(task_id)
    if event_type is not None:
        clauses.append("event_type = %s")
        params.append(event_type)
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM event_free_form {where} "
            f"ORDER BY created_at ASC, id ASC {limit_sql}",
            params,
        )
        rows = cur.fetchall()
    if not getattr(conn, "autocommit", True):
        conn.commit()
    return [_to_free_form(r) for r in rows]


__all__ = ["FreeForm", "KINDS", "record_free_form", "read_free_form"]
