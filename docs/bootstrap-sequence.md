# Bootstrap sequence — how the host cold-starts and spawns agents

This is the **genesis runbook**: the sequence the future local agent (and the
human) follows on a fresh clone of this repo on the target Mac. It answers "how do
all the agents get spawned?" — see [ADR-0009](decisions/0009-agent-lifecycle-and-genesis.md)
for the lifecycle model.

> **Status:** target sequence. None of this is built yet, and the milestones do
> **not** map one-to-one onto the layers below. **M0** (the next milestone)
> delivers **Layer 0 only** — the infra spine. **Layer 1** (supervisor +
> scheduler) and **Layers 2–3** (PM pulse + on-demand roles) land in later
> milestones (M1–M3), because the supervisor needs the event/task model first.
> The *first* genesis task is "build M0" — see
> [`../state/inbox/0001-genesis.md`](../state/inbox/0001-genesis.md).

## The mental model

Do **not** boot a fleet of agent daemons. Keep a handful of **singletons**
always-on; spawn every **role agent on-demand per task** and tear it down after.
"Spawn an agent" = "enqueue a task."

```
Layer 0  Infra          docker compose up   → Postgres, Redis, Qdrant, MinIO,
                                              OTel, Prometheus, Grafana
Layer 1  Guarantees     start supervisor (non-agent) + scheduler (cron/launchd)
Layer 2  Root pulse     ensure the Productivity PM runs on a schedule
Layer 3  On-demand      PM reads goals → enqueues tasks → runtime materializes
                        Executor / Reviewer / Retro / Researcher per task
         Interface      Spokesman reports state; stakeholder replies → events
```

## Step by step (target)

1. **Clone & configure.** `git clone`, copy `.env.example` → `.env`, fill secrets
   (never committed). `PREREQS.md` lists the container runtime + versions.
2. **Layer 0 — infra.** `./bootstrap` (or `make up`) runs `docker compose up -d`
   and a **health check** that self-verifies every container (so the clone
   confirms itself; the remote session can't).
3. **Layer 1 — guarantees.** Start the **supervisor** (a small always-on non-LLM
   loop: re-kick singletons + tasks with stale heartbeats) and the **scheduler**.
4. **Layer 2 — root pulse.** Register the **Productivity PM tick** with the
   scheduler. The PM is not a daemon; it wakes on a cadence (the studio heartbeat),
   does bounded work, and exits — the supervisor guarantees it keeps waking.
5. **Layer 3 — the PM spawns the rest.** On each tick the PM:
   - reads goals: `state/status.md`, `state/inbox/`, long-term memory;
   - runs its **confidence gate** on any new/changed objective (or asks the
     stakeholder via the Spokesman, per [ADR-0006](decisions/0006-stakeholder-comms.md));
   - **enqueues tasks**; the runtime materializes the right role agent for each,
     gated by policy and routed by the model router;
   - checks in-flight work, nudges stalls, escalates 🛑/🚨 as needed.
6. **Interface.** The **Spokesman** is ensured-running; it aggregates state from
   the event log and pushes to the dashboard + WhatsApp; replies re-enter as
   events/tasks.

## Recovery & liveness

- Every task and singleton writes a **heartbeat**. The supervisor re-kicks
  anything stale — this is the only thing that must never itself silently die
  (keep it tiny; run it under launchd so the OS restarts it).
- A reboot of the Mac replays cleanly: `docker compose up` + supervisor start
  resumes from Postgres state; in-flight tasks resume from their last checkpoint.

## What this maps to in code (once M0 exists)

| Layer | Artifact (planned) |
| --- | --- |
| 0 | `docker-compose.yml`, `bootstrap`/`Makefile`, `scripts/healthcheck` |
| 1 | `supervisor/` (non-LLM loop), launchd/cron unit |
| 2 | PM tick registration + `roles/pm/` (prompt + skills) |
| 3 | `runtime/` task dispatch, `roles/{executor,reviewer,retro,researcher}/` |
| — | `spokesman/` (dashboard + WhatsApp) |
