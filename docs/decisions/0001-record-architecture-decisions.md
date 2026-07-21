# 0001 — Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

AI Studio starts as a design (see [`../architecture.md`](../architecture.md))
that will evolve over months. We need a lightweight, durable record of *why*
each significant choice was made, so future changes (by a human or an agent) can
tell an intentional decision from an accident.

## Decision

We keep **Architecture Decision Records (ADRs)** as numbered markdown files in
`docs/decisions/`. Each records one decision with: context, the decision, and
consequences. Superseded ADRs are marked as such rather than deleted.

Any change that would violate an invariant in `CLAUDE.md` or alter the
architecture in `docs/architecture.md` must be accompanied by a new ADR.

## Consequences

- Decisions are auditable and replayable — consistent with the project's own
  "observable, replayable" philosophy.
- Small overhead per decision; worth it for a long-lived system.
