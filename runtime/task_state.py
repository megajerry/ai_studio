"""The canonical task-lifecycle state machine (ADR-0015).

This is the ONE place the studio's task states + legal transitions are defined.
Every task state change in the runtime goes through :func:`runtime.tasks.transition`,
which consults :func:`assert_transition` here — there are **no ad-hoc status
UPDATEs** anywhere else. Keeping the machine in a single, DB-free module means the
fleet knows the lifecycle even when Postgres is down (importing this never touches
a database).

Lifecycle (the dev/review flow, unified with the runtime loop):

    up_for_grabs → claimed → in_progress → ready_for_review → approved → merged
                                    │              │
                                    │ (🔴 approval)│ (verify/reviewer fail)
                                    ▼              ▼
                                 blocked     reviewer_blocked
                                    │              │
                                    └──────────────┴──► in_progress (retry)

``merged`` and ``abandoned`` are terminal. ``abandoned`` is reachable from every
non-terminal state (any unit of work can be dropped).

Beyond the forward lifecycle above (exactly the ADR-0015 set), two **operational
recovery edges** return dropped/parked work to the grab pool so the grab-based
loop can re-service it — these are the liveness layer (ADR-0004), not part of the
normal forward flow:

- ``in_progress → up_for_grabs`` — the non-agent supervisor re-kicks a task whose
  worker went silent (stale heartbeat).
- ``blocked → up_for_grabs`` — a task parked on a 🔴 approval is re-queued once a
  human grants it, so a fresh worker re-runs the action and finds the grant.
"""

from __future__ import annotations

from .models import TaskStatus

# --- Canonical states -------------------------------------------------------

#: The full canonical set (string values), for CHECK constraints + validation.
STATES: frozenset[str] = frozenset(s.value for s in TaskStatus)

#: Terminal states — no outgoing transitions.
TERMINAL: frozenset[str] = frozenset({TaskStatus.MERGED.value, TaskStatus.ABANDONED.value})


# --- Legal transitions ------------------------------------------------------

#: The canonical forward lifecycle (ADR-0015) plus the two operational recovery
#: edges (documented above). ``transition()`` rejects anything not listed here.
TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.UP_FOR_GRABS.value: {TaskStatus.CLAIMED.value, TaskStatus.ABANDONED.value},
    TaskStatus.CLAIMED.value: {
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.UP_FOR_GRABS.value,
        TaskStatus.ABANDONED.value,
    },
    TaskStatus.IN_PROGRESS.value: {
        TaskStatus.READY_FOR_REVIEW.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.ABANDONED.value,
        # operational recovery: supervisor re-kick of a stale worker (ADR-0004).
        TaskStatus.UP_FOR_GRABS.value,
    },
    TaskStatus.BLOCKED.value: {
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.ABANDONED.value,
        # operational recovery: re-queue once a 🔴 approval is granted.
        TaskStatus.UP_FOR_GRABS.value,
    },
    TaskStatus.READY_FOR_REVIEW.value: {
        TaskStatus.APPROVED.value,
        TaskStatus.REVIEWER_BLOCKED.value,
        TaskStatus.ABANDONED.value,
    },
    TaskStatus.REVIEWER_BLOCKED.value: {
        TaskStatus.IN_PROGRESS.value,
        TaskStatus.ABANDONED.value,
    },
    TaskStatus.APPROVED.value: {TaskStatus.MERGED.value, TaskStatus.ABANDONED.value},
    TaskStatus.MERGED.value: set(),
    TaskStatus.ABANDONED.value: set(),
}


class IllegalTransition(ValueError):
    """Raised when a state change is not permitted by :data:`TRANSITIONS`."""


def _val(status) -> str:
    return status.value if isinstance(status, TaskStatus) else str(status)


def can_transition(from_status, to_status) -> bool:
    """True if ``from_status → to_status`` is a legal transition."""
    return _val(to_status) in TRANSITIONS.get(_val(from_status), set())


def assert_transition(from_status, to_status) -> None:
    """Raise :class:`IllegalTransition` unless ``from_status → to_status`` is legal."""
    frm, to = _val(from_status), _val(to_status)
    if frm not in TRANSITIONS:
        raise IllegalTransition(f"unknown source status {frm!r}")
    if to not in STATES:
        raise IllegalTransition(f"unknown target status {to!r}")
    if to not in TRANSITIONS[frm]:
        raise IllegalTransition(
            f"illegal transition {frm!r} → {to!r} "
            f"(allowed: {sorted(TRANSITIONS[frm]) or 'none — terminal'})"
        )


def is_terminal(status) -> bool:
    """True if ``status`` is a terminal state (``merged`` / ``abandoned``)."""
    return _val(status) in TERMINAL


# --- Dependency-graph safety (task DAG) -------------------------------------


class DependencyCycle(ValueError):
    """Raised when task dependency edges would form a cycle / self-dependency."""


def assert_acyclic(edges: dict[int, list[int]]) -> None:
    """Reject cycles / self-deps in a proposed dependency graph (keyed by index).

    ``edges`` maps a node to the list of nodes it depends on (its prerequisites).
    Used by the PM before it enqueues a decomposition so a cyclic plan is rejected
    with a clear error rather than producing tasks that can never be grabbed.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[int, int] = {n: WHITE for n in edges}

    def visit(n: int, stack: list[int]) -> None:
        color[n] = GRAY
        for dep in edges.get(n, []):
            if dep == n:
                raise DependencyCycle(f"task {n} depends on itself")
            if color.get(dep, WHITE) == GRAY:
                cyc = " → ".join(str(x) for x in stack + [n, dep])
                raise DependencyCycle(f"dependency cycle: {cyc}")
            if color.get(dep, WHITE) == WHITE:
                visit(dep, stack + [n])
        color[n] = BLACK

    for node in edges:
        if color[node] == WHITE:
            visit(node, [])
