# 0010 — Off-host agent: an intermittent remote worker

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

The execution **host** (the Mac) is not the only place work can happen. There is
also an **off-host agent**: a capable agent session (e.g. Claude Code) running on
a *different* machine, with no direct connection to the host — it shares state
only through the git substrate ([ADR-0007](0007-cross-machine-state.md)). This
session that authored the bootstrap **is** the first such off-host agent.

Its defining properties:

- **Intermittent** — not always active or responsive; may act hours or days after
  a request, or not at all.
- **Git-only** — it can neither see the host's running services/DB nor hold host
  secrets; it works entirely from what's committed to the repo.
- **Compute-elastic** — useful to **offload work when the host is compute-
  constrained**, or for work that needs no host-local resources (research, design,
  doc/spec authoring, code drafting, code review).

## Decision

Treat the off-host agent as a **first-class but best-effort worker**. Work is
delegated **asynchronously via git**, never on a blocking path:

- `state/offhost/requests/` — the host/PM drops **self-contained** task requests
  (id, goal, inputs, acceptance criteria, priority, "stale after" hint). They must
  be idempotent and complete, since the off-host agent may act much later with
  only git as context.
- `state/offhost/results/` — the off-host agent writes results back (typically a
  reference to a pushed branch/PR plus a summary).
- The off-host agent, when active, pulls, **claims** a request (writes a claim so
  work isn't duplicated), does it on a branch under the normal lifecycle
  ([CONTRIBUTING.md](../../CONTRIBUTING.md)), pushes, and posts a result.

**The host must never depend on it.** Any delegated item has a host-side fallback
(do it locally, or wait) and a timeout — because responsiveness is not guaranteed.

The PM uses this as a **capacity lever**: route non-urgent, host-resource-free
tasks to `offhost` when local compute is tight; keep urgent, interactive, or
host-local (services/secrets/deploy) work on the host.

## Consequences

- Extra effective compute at zero host load, for the right kind of tasks.
- Requests must be written to survive long latency and total context loss — which
  reinforces the "repo is self-sufficient memory" principle.
- A claim/heartbeat convention is needed to avoid duplicate work and to reclaim
  abandoned claims (the supervisor can expire stale off-host claims).
- Merge review still applies: off-host work lands via reviewed PRs like any other.
