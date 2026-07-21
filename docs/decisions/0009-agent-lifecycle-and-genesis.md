# 0009 — Agent lifecycle: ensured singletons + on-demand task-spawned roles

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

We need a clear answer to "how does the host start, and how do all the agents get
spawned?" A naive approach boots a fixed fleet of long-running agent daemons —
which is expensive, fragile, and contradicts "agents are stateless executors."

## Decision

Two classes of process:

**1. Always-on singletons (few, ensured-running).** These are the studio's pulse
and are kept alive by the bootstrap entrypoint + the supervisor:

- infra containers (Postgres/Redis/Qdrant/observability);
- the **supervisor** (non-agent liveness guarantee — [ADR-0004](0004-agent-driven-orchestration.md));
- a **scheduler** (cron/launchd) for wake-ups;
- the **Productivity PM pulse** — a scheduled tick that ensures the root PM runs;
- the **Spokesman** — the stakeholder interface.

**2. On-demand, task-spawned roles (everything else).** Executor, Reviewer,
Retro, Researcher, and all vertical-workstream agents are **not** daemons. They
are materialized **per task**, run to completion (or checkpoint), emit events, and
are torn down. "Spawning an agent" = **enqueue a task**; the runtime/supervisor
materializes the agent to service it. No agent spawns another by direct call —
coordination is via the queue/event log only.

**Genesis (cold start).** The bootstrap entrypoint stands up infra → supervisor →
scheduler, then **ensures the Productivity PM exists**. The PM reads the studio's
goals (`state/status.md`, `state/inbox/`, long-term memory), decides what
workstreams/tasks are needed, and enqueues them; the runtime dispatches role
agents per task, gated by the policy engine ([architecture §5](../architecture.md))
and routed by the model router ([ADR-0005](0005-model-registry-router.md)).

## Consequences

- Minimal always-on footprint; the studio scales by **tasks, not daemons**.
- The **PM pulse cadence is the studio's heartbeat**; it must be cheap and
  reliable (top-tier model, tight prompt, budgeted context).
- Recovery is uniform: the supervisor re-kicks dead singletons **and** orphaned
  tasks.
- Requires a documented, automatable cold-start sequence — see
  [`../bootstrap-sequence.md`](../bootstrap-sequence.md).
