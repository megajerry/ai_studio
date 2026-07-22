# Task lifecycle — states, fleet, grab/transition API, telemetry

The canonical lifecycle every task moves through, and how the fleet (PM →
Executors → Verifier → human/off-host Reviewer + the non-agent supervisor)
operates it. Defined once in [`runtime/task_state.py`](../runtime/task_state.py)
(states + legal transitions, DB-free so it is known even when Postgres is down)
and enforced by the single guard `runtime.tasks.transition`. Decision record:
[ADR-0015](decisions/0015-task-lifecycle-state-machine.md).

## States

| State | Meaning |
| --- | --- |
| `up_for_grabs` | Created / released; grabbable once its prerequisites are `merged`. |
| `claimed` | A worker grabbed it (owns the claim) but has not started. |
| `in_progress` | Being worked (heartbeating). |
| `blocked` | Parked on a pending 🔴 human approval. |
| `ready_for_review` | Executor submitted an artifact; awaiting review. |
| `reviewer_blocked` | Review failed; needs rework (retry) or abandonment. |
| `approved` | Review passed; ready to merge. |
| `merged` | **Terminal — success.** |
| `abandoned` | **Terminal — dropped** (exhausted retries / denied / infeasible). |

## State diagram

```
                        ┌─────────────── abandoned (terminal) ───────────────┐
                        │        (every non-terminal state can abandon)       │
                        ▼                                                      │
   up_for_grabs ──► claimed ──► in_progress ──► ready_for_review ──► approved ─► merged
        ▲              │             │  ▲             │                          (terminal)
        │              │             │  │             ▼
        │ (recovery)   │        (🔴) │  │       reviewer_blocked
        └──────────────┘         blocked │             │
        ▲                           │    └─────────────┘  (retry → in_progress)
        │ (recovery: re-queue       │
        │  once approval granted)   │
        └───────────────────────────┘
```

**Forward flow** is the ADR-0015 set. Two **recovery edges** (the liveness layer,
not the normal flow): `in_progress → up_for_grabs` (supervisor re-kick of a stale
worker) and `blocked → up_for_grabs` (re-queue after a 🔴 grant).

## Fleet operating model

- **PM** (`roles/pm.py`) decomposes a goal into work items and enqueues them as
  `up_for_grabs`, setting dependency edges (`depends_on`) so independent items run
  in parallel and dependents wait. Cyclic / self-referential plans are rejected.
- **Worker / Executor** (`worker.py`) runs one task to a terminal state in a
  single pass: grab → start → Executor → submit → **Verifier**. The **Verifier is
  the automated reviewer** (`ready_for_review → approved → merged` on pass;
  `→ reviewer_blocked → in_progress` retry, else `→ abandoned`). A 🔴 tool call
  parks the task `blocked`; `resume_approved` re-queues it after a human grant.
- **Reviewer (human / off-host)** queries `list_for_review(conn)` and performs
  `ready_for_review → approved | reviewer_blocked` via `transition` — the same
  guarded path the Verifier uses.
- **Supervisor** (`supervisor.py`, non-agent) re-kicks stale `claimed`/`in_progress`
  tasks back to `up_for_grabs` and force-abandons ones that exhaust their retries.

## Grab + transition API (`runtime/tasks.py`)

```python
# Grab-by-sort: pick one grabbable up_for_grabs task → claimed. SORT/FILTER are
# supplied by the spawning agent. FOR UPDATE SKIP LOCKED → no double-grab.
grab_task(conn, *, worker_id, agent_type=None, assignee=None,
          sort="priority DESC, created_at ASC", filter=None, workstream=None)

start_task(conn, task_id, worker_id)          # claimed → in_progress
claim_task(conn, *, worker_id, ...)           # convenience = grab_task + start_task

# THE single guarded state change (telemetry row + task.transition event).
transition(conn, task_id, to, *, agent_id=None, agent_type=None,
           expected_from=None, result=None, spent_tokens=None, ...)

complete_task(conn, task_id, *, status=MERGED|ABANDONED, ...)  # internal tasks
block_task / requeue_blocked_task / rekick_task                # wrappers over transition

list_for_review(conn, *, workstream=None)     # tasks awaiting a Reviewer
```

An illegal move raises `IllegalTransition`; the UPDATE is guarded on the current
status (a concurrent change is a no-op → `None`).

## Dependencies — what's parallelizable vs blocked

```python
ready_tasks(conn, *, workstream=None)   # up_for_grabs with ALL prereqs merged — grab now, in parallel
waiting_tasks(conn, *, workstream=None) # up_for_grabs blocked by unmet/abandoned prereqs
                                        # → [{task, pending_prereqs, blocked_by_abandoned}]
```

A task is grabbable only when `up_for_grabs` **and** every prerequisite is
`merged`. An `abandoned` prerequisite means the dependent can never run — it stays
in `waiting_tasks` (surfaced, never silently grabbed).

## Telemetry queries (extends ADR-0012)

Every transition appends a `task_transitions` row (`from`/`to`/`agent_id`/
`agent_type`/`at`/`latency_ms`) and emits a `task.transition` event.

```python
task_lifecycle(conn, task_id)  # {transitions:[…], total_ms, current, depends_on}
task_cost(conn, task_id)       # {calls, input/output/cached/total_tokens, cost_usd, latency_ms, spent_tokens}
agent_rollup(conn)             # per (agent_type, to_status): count + avg latency
model_rollup(conn)             # per model: calls, avg latency, total cost + tokens
```

Cost links per task because every `call_model` on a task's behalf carries its
`task_id`. Events and telemetry carry ids / statuses / counts only — never secret
or argument text (invariants 5 & 6).

## Not in scope (separate follow-up)

DB-outage resilience and remote host-restricted DB access are deliberately out of
scope for this milestone.
