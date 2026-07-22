"""AI Studio runtime — event log + task queue (M1) + policy engine & tools (M2).

The coordination substrate every agent uses (ADR-0004/0009/0010/0012): an
append-only event log and a task queue (M1), plus the capability-gated tool
layer (M2) — the policy engine (architecture §5), the tool abstraction/registry,
and the enforced ``invoke`` path that is the ONLY way an agent runs a tool.
Agents never call each other and never touch the host directly.
"""

from .capabilities import (
    DEFAULT_CAPABILITY_TIER,
    ActionTier,
    Capability,
    effective_tier,
)
from .db import can_connect, connect
from .enforce import (
    DbEventSink,
    InvokeResult,
    InvokeStatus,
    MemoryEventSink,
    NullEventSink,
    invoke,
)
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
from .policy import (
    BudgetContext,
    Decision,
    Effect,
    PolicyConfig,
    PolicyRequest,
    decide,
    load_policy,
)
from .tasks import (
    claim_task,
    complete_task,
    enqueue_task,
    find_stale_tasks,
    heartbeat,
)
from .tools import (
    FilesystemTool,
    ShellTool,
    Tool,
    ToolRegistry,
    ToolResult,
)

__all__ = [
    # M1 — event log + task queue
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
    # M2 — capabilities & tiers
    "ActionTier",
    "Capability",
    "DEFAULT_CAPABILITY_TIER",
    "effective_tier",
    # M2 — policy engine
    "BudgetContext",
    "Decision",
    "Effect",
    "PolicyConfig",
    "PolicyRequest",
    "decide",
    "load_policy",
    # M2 — tools & registry
    "FilesystemTool",
    "ShellTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    # M2 — enforced invocation path
    "DbEventSink",
    "InvokeResult",
    "InvokeStatus",
    "MemoryEventSink",
    "NullEventSink",
    "invoke",
]
