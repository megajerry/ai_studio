# CLAUDE.md — working notes for AI Studio

This file orients any AI assistant (or human) working in this repo. Read it
before making changes.

## What this is

AI Studio is an **event-driven, local-first AI operating system** for running a
one-person Venture Studio. **This repo is the Productivity workstream** — the one
horizontal platform every vertical draws on (ADR-0002). The full design lives in
[`docs/architecture.md`](docs/architecture.md); decisions are logged in
[`docs/decisions/`](docs/decisions/). Current studio state is in
[`state/status.md`](state/status.md). Read those before proposing changes.

**North star (do not violate without an explicit decision record):**

> Agents are stateless executors. Workflows are deterministic orchestrators.
> Tools are capability-gated. Every action is observable, replayable, and
> reversible.

## Invariants that constrain how code is written

These are architectural constraints, not style preferences. If a change would
break one, stop and flag it.

1. **Agents don't call agents.** Coordination happens via the task queue / event
   log only. No direct agent-to-agent function calls.
2. **Agents don't touch the host.** No direct `subprocess`, `os.system`, network
   sockets, or filesystem writes from agent code. All side effects go through a
   **tool**.
3. **Tools are permissioned.** Every tool declares the capabilities it needs;
   the policy engine gates each call (read? write? approve? budget?).
4. **Mutations go through verify → commit.** No agent writes state directly;
   changes flow `Planner → Task Graph → Executor → Verifier → Commit`.
5. **Secrets never reach an agent.** They live in the secret manager; tools call
   external services on the agent's behalf.
6. **Everything emits events + traces.** Any new action must be observable and
   replayable from the event log.
7. **Local-first.** The Mac is the source of truth; cloud components are
   stateless workers.

## Approval tiers (enforced by the policy engine)

- 🟢 **Green** — read / search / summarize → auto.
- 🟡 **Yellow** — git commit / write file / create branch → auto but logged.
- 🔴 **Red** — delete / publish / spend money / SSH / deploy / pay → human
  approval required.

## Tech stack (candidate — confirm against docs/decisions before assuming)

- **Language:** Python (primary). JS/TS only for MCP servers or dashboards.
- **Agent runtime:** OpenAI Agents SDK / PydanticAI.
- **Orchestration:** agent-driven (PM) + a minimal non-agent **supervisor**
  (Postgres state + heartbeats + cron/launchd). **Temporal is the upgrade path,
  not a day-one dependency** — see ADR-0004.
- **Roles:** each `= prompt + skills + tools`. Skills use the Agent Skills open
  standard (ADR-0008); curate + review before use.
- **Tools:** MCP servers; Playwright for browser.
- **State/memory:** PostgreSQL + Redis + Qdrant.
- **Observability:** OpenTelemetry + Grafana + Prometheus.
- **Sandbox/infra:** Docker Compose (no Kubernetes in phase 1).
- **Coding worker:** opencode, treated as a replaceable Worker.
- **Cross-machine (dev):** `state/` in git is the shared substrate until the host
  is live (ADR-0007).

## Development lifecycle (mandatory)

Agents and humans both follow the loop in [`CONTRIBUTING.md`](CONTRIBUTING.md):
**branch → iterate (commit often) → review-agent approves → merge → delete
branch.** Never commit to `main`; branch as `<agent-workflow-identity>/<task-summary>`
(e.g. `builder/search-tool`). **Commit early and often on your branch and push it**
— those in-branch commits are progress snapshots and revert points and need no
review; **only the merge to `main` triggers a review round**, done by a *separate*
review agent (never self-approve). A change isn't done until a fresh `git clone`
on the target machine can bootstrap and run it.

## Local dev

Phase 1 target is a single `docker compose up` on the Mac. Commands will be
documented here as they land. Until then, there is no build/test/run command.

## Conventions

- Prefer assembling mature open-source components over writing our own.
- Match the style and idioms of surrounding code.
- Record any architecture-affecting decision as an ADR in `docs/decisions/`.
