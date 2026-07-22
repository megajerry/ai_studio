"""AI Studio runtime — Postgres-backed event log + task queue (M1).

The coordination substrate every agent uses (ADR-0004/0009/0010/0012): an
append-only event log and a task queue. Agents never call each other directly;
they enqueue tasks and read/append events here.
"""

from .db import can_connect, connect
from .events import append_event, read_events
from .models import (
    Assignee,
    Event,
    EventIn,
    EventType,
    Task,
    TaskStatus,
    build_database_url,
    is_stale,
    make_event,
)
from .tasks import (
    claim_task,
    complete_task,
    enqueue_task,
    find_stale_tasks,
    heartbeat,
)

__all__ = [
    "Assignee",
    "Event",
    "EventIn",
    "EventType",
    "Task",
    "TaskStatus",
    "append_event",
    "build_database_url",
    "can_connect",
    "claim_task",
    "complete_task",
    "connect",
    "enqueue_task",
    "find_stale_tasks",
    "heartbeat",
    "is_stale",
    "make_event",
    "read_events",
]
