# 0004 — Agent-driven orchestration with a minimal non-agent supervisor

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

We debated whether orchestration reliability (don't stop early, verify "done",
survive crashes, resume long tasks) must live in a deterministic workflow engine
(e.g. Temporal), or whether a capable PM agent using tools (cron/scheduling,
spawning independent verifiers, a state store) can provide it.

Conclusion from that discussion: the PM *can* provide liveness (via scheduled
wake-ups, not in-session busy-waiting) and separation-of-judgment (via an
independent verifier agent). Cron itself is already a *non-agent durability
primitive* — so the real axis is not "agent vs engine" but **how heavy the
non-agent guarantee must be:** `raw cron → thin supervisor → Temporal`.

The one thing an agent cannot guarantee for itself: that its own safety net was
set up correctly *before* it crashed/hallucinated/ran out of budget (the
crash-before-checkpoint gap).

## Decision

Orchestration is **agent-driven**: the PM plans, decides when/what to nudge,
spawns independent verifiers, and schedules its own wake-ups.

Reliability the agent cannot self-provide comes from **one minimal non-agent
component: a supervisor** — a dumb, always-on, non-LLM loop whose only job is
*"no task is silently dropped."* It scans for in-progress tasks with a stale
heartbeat and re-kicks them.

We **start with the thin supervisor** (Postgres state + heartbeats + cron/launchd
scheduler + supervisor loop). **Temporal is the documented upgrade path**, adopted
only when hand-rolled retry/replay/backoff/human-approval logic starts getting
subtly wrong — not a day-one dependency.

## Consequences

- Lighter phase-1 infra; no workflow-engine dependency to start.
- The supervisor is small but **mandatory** — it is the irreducible non-agent
  guarantee; it must be simple, well-tested, and itself monitored.
- Tasks must write heartbeats and enough state for a fresh agent to resume.
- Migration to Temporal must be anticipated (keep orchestration logic isolated
  behind an interface so it can be swapped).

## Supersedes

The earlier implicit assumption ("Temporal from day one") from initial scoping.
