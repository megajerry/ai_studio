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
# NOTE: `runtime.supervisor` / `runtime.scheduler` are deliberately NOT imported
# here — they are always-on entrypoints (`python -m runtime.supervisor`), and
# re-exporting them from the package triggers a double-import RuntimeWarning
# under `-m`. Import `sweep` / `tick_once` from those submodules directly.
from .task_state import (
    TRANSITIONS,
    DependencyCycle,
    IllegalTransition,
    assert_transition,
    can_transition,
    is_terminal,
)
from .tasks import (
    add_spent_tokens,
    agent_rollup,
    claim_task,
    complete_task,
    enqueue_task,
    find_stale_tasks,
    grab_task,
    heartbeat,
    list_for_review,
    model_rollup,
    ready_tasks,
    rekick_task,
    start_task,
    task_cost,
    task_lifecycle,
    transition,
    waiting_tasks,
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
    "add_spent_tokens",
    "agent_rollup",
    "append_event",
    "build_database_url",
    "can_connect",
    "claim_task",
    "complete_task",
    "connect",
    "enqueue_task",
    "find_stale_tasks",
    "grab_task",
    "heartbeat",
    "is_stale",
    "list_for_review",
    "make_event",
    "model_rollup",
    "read_events",
    "ready_tasks",
    "rekick_task",  # M3a supervisor re-kick primitive (loop lives in runtime.supervisor)
    "start_task",
    "task_cost",
    "task_lifecycle",
    "transition",
    "waiting_tasks",
    # M3d — canonical lifecycle state machine (ADR-0015)
    "TRANSITIONS",
    "DependencyCycle",
    "IllegalTransition",
    "assert_transition",
    "can_transition",
    "is_terminal",
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
