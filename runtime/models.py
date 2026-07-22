"""Typed models + pure logic for the event log and task queue (M1).

Everything in this module is DB-free and unit-testable: the pydantic row models,
the ``DATABASE_URL`` builder, the event-envelope constructor, and the stale-task
predicate. The data-access modules (:mod:`runtime.events`, :mod:`runtime.tasks`)
build on these; the migration schema is in ``runtime/migrations/``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, Field


# --- Enumerations -----------------------------------------------------------


class TaskStatus(str, Enum):
    """Lifecycle states of a queued unit of work."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class Assignee(str, Enum):
    """Where a task is meant to run (ADR-0010). ``None`` means any worker."""

    HOST = "host"
    OFFHOST = "offhost"


# Canonical event types emitted by the task lifecycle. Kept as constants so
# producers/consumers agree on the wire strings (the column itself is free-form
# text so other subsystems can emit their own event types).
class EventType(str, Enum):
    TASK_CREATED = "task.created"
    TASK_CLAIMED = "task.claimed"
    TASK_HEARTBEAT = "task.heartbeat"
    TASK_FINISHED = "task.finished"


# --- Row models -------------------------------------------------------------


class EventIn(BaseModel):
    """An event ready to be appended (no id/ts — the DB assigns those)."""

    workstream: str
    type: str
    task_id: Optional[UUID] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


class Event(BaseModel):
    """A persisted, immutable event log row."""

    id: UUID
    ts: datetime
    task_id: Optional[UUID] = None
    workstream: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


class Task(BaseModel):
    """A persisted task-queue row."""

    id: UUID
    workstream: str
    type: str
    status: TaskStatus
    priority: int
    assignee: Optional[Assignee] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    heartbeat_at: Optional[datetime] = None
    claimed_by: Optional[str] = None
    budget_tokens: Optional[int] = None
    spent_tokens: int = 0
    created_at: datetime
    updated_at: datetime


# --- Pure helpers -----------------------------------------------------------


def build_database_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Resolve the Postgres connection URL.

    Uses ``DATABASE_URL`` verbatim when set; otherwise assembles one from the
    ``POSTGRES_*`` variables (same defaults as ``docker-compose.yml``). The
    password is URL-encoded so special characters survive.
    """
    env = os.environ if env is None else env

    url = env.get("DATABASE_URL")
    if url:
        return url

    user = env.get("POSTGRES_USER", "aistudio")
    db = env.get("POSTGRES_DB", "aistudio")
    password = env.get("POSTGRES_PASSWORD", "")
    host = env.get("POSTGRES_HOST", "localhost")
    port = env.get("POSTGRES_PORT", "5432")

    if password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    else:
        auth = f"{quote(user, safe='')}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


def make_event(
    *,
    workstream: str,
    type: str,
    task_id: Optional[UUID] = None,
    payload: Optional[dict[str, Any]] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> EventIn:
    """Construct an event envelope with defaults filled in.

    Centralizing envelope construction keeps every producer consistent
    (workstream + type always set, payload defaults to ``{}``).
    """
    if not workstream:
        raise ValueError("event workstream must be non-empty")
    if not type:
        raise ValueError("event type must be non-empty")
    return EventIn(
        workstream=workstream,
        type=type,
        task_id=task_id,
        payload=payload or {},
        trace_id=trace_id,
        span_id=span_id,
    )


def is_stale(
    task: Task,
    threshold_seconds: float,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Return True if an in-progress task's heartbeat is older than the threshold.

    This is the exact predicate the non-agent supervisor (ADR-0004) uses to
    decide a task was silently dropped and must be re-kicked. Only
    ``in_progress`` tasks are considered; a missing heartbeat counts as stale.
    """
    if task.status != TaskStatus.IN_PROGRESS:
        return False
    if task.heartbeat_at is None:
        return True

    now = datetime.now(timezone.utc) if now is None else now
    heartbeat = task.heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    age = (now - heartbeat).total_seconds()
    return age > threshold_seconds
