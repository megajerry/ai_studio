# 0002 — Productivity is the horizontal platform workstream

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

The Venture Studio will run many **vertical** workstreams (a research effort, a
game, a video channel, a product), each optimizing its own domain. Across all of
them there are recurring needs: choosing/routing models, PM/review/retro/research
roles, a learning corpus, and a stakeholder channel. Re-implementing these per
vertical would be wasteful and inconsistent.

## Decision

**This repository is the Productivity workstream — the single horizontal
platform.** Its product is *other agents' effectiveness*. It owns the shared
services (orchestration substrate, role templates, model registry/router,
memory/lessons, tools/policy, observability, the Spokesman) that verticals are
instantiated on top of. It is the only workstream permitted to optimize *across*
workstreams, which it does by reading their event logs.

## Consequences

- Verticals stay focused on their domain; cross-cutting concerns have one owner.
- Productivity must expose clean, reusable interfaces (role templates, skills,
  tools) rather than bespoke one-offs.
- Productivity needs read access to every workstream's event log to pull signal;
  this must respect the policy engine and least-privilege.
