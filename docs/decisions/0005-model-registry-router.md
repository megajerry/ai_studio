# 0005 — Model registry, router & sourcing

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

Model options evolve fast. Picking and routing models well (capability/quality vs
$/token vs latency) is a continuous job, and requests must route to the right
model **consistently and reliably** based on the nature of the request.

## Decision

Productivity owns three components:

- **Registry** — a versioned catalog of models with capability-by-task-type,
  $/token, latency, context window, required keys, and **provenance + date**.
- **Sourcing agent** — continuously researches credible sources (LMArena,
  provider pricing/docs) and proposes registry updates **through the normal
  PR + review loop**, so changes are traceable and reviewable.
- **Router** — given `(task type, quality bar, budget, latency SLA)` from the PM,
  selects a model via a **routing policy expressed as data, not code**, with
  fallback chains, and **logs every routing decision as an event**.

**Approval envelope** (ties to [ADR-0006](0006-stakeholder-comms.md)):

- 🛑 **Approval required** — changes that redefine objectives or *increase budget*.
- 📣 **Auto-adopt + inform** — swaps within an approved cost/quality policy band.

## Consequences

- Routing is consistent and replayable (decisions are events).
- The registry is provider-agnostic; keys are just `.env` entries and no agent
  code cares which providers are present.
- Budget enforcement is shared between the router and the policy engine.
- The sourcing agent needs the Search gateway and web access.

## References

- Best practice "match models to roles" and "budget the supervisor's context"
  from 2026 orchestration surveys (see [ADR-0003](0003-workstream-operating-model.md)).
