# AI Studio

> **Build an event-driven, local-first AI operating system where agents are
> stateless executors, workflows are deterministic orchestrators, tools are
> capability-gated, and every action is observable, replayable, and reversible.**

AI Studio is the foundation for a one-person **Venture Studio** — a system that
can research, prototype, test, ship, and operate small products with heavy AI
assistance, while a human stays in the loop for anything risky.

**This repo is the Productivity workstream** — the one *horizontal* platform that
every *vertical* workstream (a research effort, a game, a product) draws on. Its
product is *other agents' effectiveness*: shared orchestration, role templates,
model routing, memory/lessons, tools/policy, observability, and the stakeholder
channel. See [`docs/architecture.md`](docs/architecture.md).

The guiding philosophy:

> **Agents are never trusted. Workflows can be trusted. Data must be traceable.**

We treat an agent not as an "employee" but as a *powerful but unreliable CPU*.
The reliable parts of the system are the **workflow engine, the event log, the
permission system, and the replayable execution history** — not the agent.

## Design principles

1. **Local-first.** The Mac is the source of truth. The cloud is just a worker.
2. **Event-driven.** Agents never call each other directly — they communicate
   through a task queue / event log. Easier to debug, replay, and reason about.
3. **Atomic agents.** Each agent does exactly one thing (Research *or* Code *or*
   Test *or* Deploy — never all of them mixed together).
4. **Deterministic workflows.** Orchestration lives in a workflow engine, not in
   a `while True: ask_llm()` loop.
5. **Capability-gated tools.** Agents can never `rm -rf` or shell out directly.
   They can only invoke tools, and each tool enforces least-privilege permissions.
6. **Verify before commit.** No agent mutates state directly. Every change flows
   `Planner → Task Graph → Executor → Verifier → Commit`.
7. **Human approval by risk tier** (see below).
8. **Zero trust / sandboxed.** Agents run in Docker; secrets live in a secret
   manager and are never handed to an agent — tools call on its behalf.
9. **Observable & replayable.** Metrics, traces, and an event log from day one.
10. **Assemble, don't build.** Reuse mature open-source components for every
    layer; the only thing we write from scratch is the Venture Studio logic.

## Approval tiers

| Tier      | Examples                                       | Behavior                    |
| --------- | ---------------------------------------------- | --------------------------- |
| 🟢 Green  | read, search, summarize                        | auto, no prompt             |
| 🟡 Yellow | git commit, write file, create branch          | auto, but **logged**        |
| 🔴 Red    | delete, publish, spend money, SSH, deploy, pay | **requires human approval** |

## Roles & orchestration

A workstream is a set of **atomic roles** (`= prompt + skills + tools`) coordinated
by **adaptive** orchestration — not a fixed pipeline:

- **PM** — supervisor; owns completion, runs a confidence gate before execution,
  may push back on unreasonable requirements; gets the best model.
- **Executor(s)** — do the domain work through tools.
- **Reviewer / Whistle-blower** — independent guard against disaster.
- **Retro** — distills durable lessons, auto-injected into future work.
- **Researcher** — mines external best-practice / skills.

Orchestration is **agent-driven** (the PM schedules its own wake-ups and spawns
independent verifiers), backed by **one minimal non-agent supervisor** whose only
job is "no task is silently dropped." Reliability the agent can't self-provide
lives in that supervisor — not in a heavy engine. Temporal is the documented
*upgrade path*, not a day-one dependency. See
[ADR-0004](docs/decisions/0004-agent-driven-orchestration.md).

## Target architecture (2026)

```
                 Claude / GPT / Gemini            ← models (via registry + router)
                          │
                 Agent Runtime — roles             ← PM / Executor / Reviewer / Retro / Researcher
                          │
     Orchestration: PM (agent-driven) + supervisor ← liveness guarantee; Temporal = upgrade path
                          │
        Policy Engine (can read? write? approve? budget?)
                          │
        MCP Tool Servers + Playwright              ← capability-gated execution
                          │
        PostgreSQL + Redis + Qdrant                ← memory & state (+ heartbeats)
                          │
        OpenTelemetry + Grafana + Prometheus       ← observability
                          │
                       Docker                      ← sandbox
                          │
                       macOS                       ← local-first host
```

### Memory is layered (read by scope, never "read everything")

```
Episode → Project → Knowledge → Long-term
```

### Search goes through a gateway, never agent-direct

```
Search Request → Policy → [Tavily | Exa | Brave | GitHub | Arxiv] → Cache → Memory
```

### Candidate stack

| Layer          | Choice (start)                       | Notes                                                  |
| -------------- | ------------------------------------ | ------------------------------------------------------ |
| Agent runtime  | OpenAI Agents SDK / PydanticAI       | typed I/O, tool calling, tracing; avoid CrewAI/AutoGPT |
| Orchestration  | Postgres + scheduler + thin supervisor | agent-driven; **Temporal is the upgrade path**       |
| Tools          | MCP servers                          | filesystem, git, browser, postgres, …                  |
| Browser        | Playwright                           | —                                                      |
| Search         | gateway over Tavily/Exa/Brave        | swap providers without touching agents                 |
| Memory         | PostgreSQL + Qdrant                  | vector + SQL covers ~90%; no Neo4j yet                 |
| Observability  | OpenTelemetry + Grafana + Prometheus | non-negotiable from day one                            |
| Sandbox        | Docker                               | no agent touches the host directly                     |
| Coding worker  | opencode (replaceable)               | just one Worker, not the runtime                       |

**opencode** is treated as the first "employee" (a Software Engineer worker) and
is **fully replaceable** — the studio's brain (workflow, memory, policy,
evaluation, business logic) stays stable even as coding agents come and go.

## Status

🚧 **Design / bootstrap.** The architecture-of-record and decision log are
written ([`docs/architecture.md`](docs/architecture.md),
[`docs/decisions/`](docs/decisions/)); current studio state lives in
[`state/status.md`](state/status.md). No runtime code yet — **M0 (infra spine)**
is next.

## Getting started

Nothing to run yet. Phase 1 will be a single `docker compose up` on the target
Mac. This repo is developed from a remote session; the target host is a separate
machine, so `state/` (git) is the cross-machine substrate until the host is live
(see [`state/README.md`](state/README.md)).
