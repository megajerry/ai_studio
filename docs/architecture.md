# AI Studio — Architecture

Canonical description of *what we're building and why*. This is the
architecture-of-record; changes to it are made via ADRs in
[`decisions/`](decisions/). It also serves as cross-machine memory (this repo is
worked on from a remote session while execution happens on a separate host — see
§10).

## 1. Thesis

> **Build an event-driven, local-first AI operating system where agents are
> stateless executors, workflows are deterministic orchestrators, tools are
> capability-gated, and every action is observable, replayable, and reversible.**

We are **not** building an agent framework. We are building the infrastructure
for a one-person **Venture Studio** that researches, prototypes, tests, ships,
and operates small products — mostly by AI, human-in-the-loop for anything risky.

> **Agents are never trusted. Workflows can be trusted. Data must be traceable.**

An agent is a *powerful but unreliable CPU*, not an employee. The trustworthy
parts are the orchestration, the event log, the permission system, and the
replayable execution history.

## 2. This repo = the Productivity workstream (a horizontal platform)

The studio is organized into **workstreams**:

- **Vertical** workstreams each own one domain (a research effort, a game, a
  video channel, a product). They optimize their own vertical.
- **Productivity** is the single **horizontal** workstream. Its product is
  *other agents' effectiveness*. It provides shared services every vertical
  draws on, and it optimizes *across* workstreams by reading their event logs.

**This repository is the Productivity workstream** ([ADR-0002](decisions/0002-productivity-horizontal-platform.md)).
The capabilities it owns (§4–§9) are the reusable platform; verticals are
instantiated on top later.

## 3. The workstream operating model

Each workstream is a set of **roles** (each an atomic agent,
`= prompt + skills + tools` — [ADR-0003](decisions/0003-workstream-operating-model.md),
[ADR-0008](decisions/0008-adopt-agent-skills-standard.md)) coordinated by
**adaptive orchestration**. The roles exist to fix three failure modes we've
observed in mainstream models:

| Failure mode | Role | Mechanism |
| --- | --- | --- |
| Stops prematurely when a nudge would finish it | **PM** | Owns completion. Defines measurable success criteria up front; the run is not "done" until an independent check confirms them; stalls trigger an auto-nudge before human escalation. |
| Executes before it understands the task | **PM** | A **confidence gate** before execution: restate the requirement, define success criteria, draft a plan, self-score confidence; execution is gated on a threshold — else clarify (ask the stakeholder) or research. |
| Doesn't learn from mistakes | **Retro + Researcher** | Retro distills lessons into a durable store that is **auto-injected at prompt-assembly time** into future work (so applying a lesson doesn't rely on the model remembering). Researcher mines external best-practice/tools into reusable skills. |
| Risk of disaster | **Whistle-blower / Reviewer** | Independent reviewer + policy-engine gating of irreversible/costly actions. Real-time guard; retro is the durable improvement. |

Key properties:

- **The PM is the highest-leverage role → it gets the highest-quality model.**
- **The PM may push back.** Not all requirements are reasonable; proposing
  pushback to the stakeholder is a first-class PM output, not a failure.
- **The cycle is adaptive, not fixed.** PM↔Execution iterates; review/retro/
  research run **async and at variable intensity** — more review when a
  workstream's recent error rate is high, more research in a fast-moving domain,
  throttled by token/time budget.
- **Atomic roles.** Each role does one kind of work; they never call each other
  directly — all coordination is via the event log / task queue.

## 4. Orchestration — agent-driven, with a minimal non-agent guarantee

Orchestration is **agent-driven**: the PM plans, decides *when* and *what* to
nudge, spawns **independent verifier agents** to judge completion, and schedules
its own future wake-ups (e.g. via cron/launchd) rather than busy-waiting inside a
live session.

Reliability that an agent **cannot** provide for itself is provided by one small
**non-agent** component: a **supervisor** — a dumb, always-on, non-LLM loop whose
only job is *"no task is ever silently dropped."* It scans for tasks that are
in-progress with a stale heartbeat and re-kicks them. This closes the gap where a
PM crashes/hallucinates/runs out of budget *before* it finishes wiring its own
safety net.

The design axis is **how much non-agent guarantee we want:**

```
raw cron  →  thin supervisor  →  Temporal
(lightest)   (our starting point)  (upgrade path)
```

We **start with the thin supervisor** (see §8). Temporal is the documented
**upgrade target** for when hand-rolled retry/replay/backoff/human-approval logic
starts getting subtly wrong — not a day-one dependency. See
[ADR-0004](decisions/0004-agent-driven-orchestration.md).

Conceptual execution pattern (agent-orchestrated, supervisor-guaranteed):

```
Task Created → Queue → PM (confidence gate) → Executor(s) via Tools
   → independent Verifier checks success criteria
   → pass? Commit : nudge/continue (bounded by budget)
   → Task Finished → Queue → PM → next task
(supervisor watches heartbeats across all of the above)
```

No role mutates state directly; changes flow through **verify → commit**.

**Lifecycle & genesis.** Only a few processes are always-on singletons (infra, the
supervisor, a scheduler, the PM pulse, the Spokesman); every role agent is spawned
**on-demand per task** and torn down after — "spawn an agent" means "enqueue a
task." How the host cold-starts and brings the studio up is specified in
[ADR-0009](decisions/0009-agent-lifecycle-and-genesis.md) and the runbook
[`bootstrap-sequence.md`](bootstrap-sequence.md).

## 5. Tool layer & permissions

Agents can never `rm -rf` or shell out. They may **only invoke tools**
(Filesystem, Browser, Git, Shell-in-sandbox, Email, Calendar, Slack, …). Each
tool declares the capabilities it needs and enforces **least privilege**:

- Research role → Browser, Read File. *No* Delete.
- Deploy role → Git, SSH, Docker. *No* Calendar.

### Policy engine

Every action passes through one policy layer that answers:

```
Can Read?  Can Write?  Need Approval?  Need Budget?  Need Retry?  Need Human?
```

Rules live in this one layer; the agent doesn't know them — it acts, and the
policy engine allows / gates / escalates. Budget enforcement lives here and in
the model router (§6).

### Action tiers (what agents may do)

| Tier | Actions | Behavior |
| --- | --- | --- |
| 🟢 Green | read, search, summarize | auto |
| 🟡 Yellow | git commit, write file, create branch | auto, logged |
| 🔴 Red | delete, publish, spend money, SSH, deploy, pay | approval required |

The 🔴 tier is what surfaces to the stakeholder as an approval (§9).

## 6. Model registry, router & sourcing

Choosing and routing models is a continuous job Productivity owns
([ADR-0005](decisions/0005-model-registry-router.md)):

- **Registry** — a versioned catalog of models: capability-by-task-type,
  $/token, latency, context window, keys required, and **provenance + date**.
- **Sourcing agent** — continuously researches credible sources (LMArena,
  provider pricing/docs) and proposes registry updates **through the normal
  PR + review loop**, so model choices are traceable and human-approvable, never
  silently drifting.
- **Router** — given `(task type, quality bar, budget, latency SLA)` from the
  PM, selects the model per a **routing policy that is data, not code**, with
  fallback chains, and **logs every routing decision as an event** (consistent,
  replayable).

Registry changes follow the approval envelope in [ADR-0005](decisions/0005-model-registry-router.md):
objective/scope-affecting or budget-increasing changes are 🛑 approval-gated;
in-band swaps within a cost/quality policy band can be auto-adopted and 📣
informed.

## 7. Memory & search

### Memory — four layers, read by scope

```
Episode → Project → Knowledge → Long-term
```

Agents never read all of memory; they read within their **scope**. The **lessons
corpus** from Retro lives in the Knowledge layer and is auto-injected into
relevant future work.

Backing store: **PostgreSQL + Qdrant** (vector + SQL covers ~90%; defer Neo4j/
graph memory).

### Search — through a gateway, never agent-direct

```
Search Request → Policy → [Tavily | Exa | Brave | GitHub | Arxiv | …] → Cache → Memory
```

All searches are cached; providers swap without touching agents.

## 8. Security, sandboxing & local-first

- **Zero trust.** No agent shells directly; everything runs in a **Docker**
  sandbox (a VM for especially sensitive work).
- **Secrets never reach an agent.** They live in a secret manager; a **tool calls
  the external service on the agent's behalf**.
- **Local-first.** The Mac is always the **source of truth**; the cloud is just a
  stateless worker (`Mac → push → Cloud Runner → run → result → Mac`, never the
  reverse).

### Phase-1 infra (Docker Compose on the Mac — no Kubernetes)

The starting substrate is deliberately light (see §4):

- **PostgreSQL** — task/event state, heartbeats, memory (SQL side)
- **Redis** — queue / cache
- **Qdrant** — vector memory
- **Scheduler** — cron/launchd wake-ups (a tool the PM uses)
- **Supervisor** — the non-LLM liveness guarantee
- **Agent Runtime** — role agents
- **Observability** — OpenTelemetry + Prometheus + Grafana (from day one)
- **MinIO** — object storage
- **Reverse proxy / tunnel** — remote access for the stakeholder channel (§9)

Migrating to a server later should require almost no changes. **Temporal** slots
in here if/when the supervisor path outgrows itself.

## 9. Stakeholder communication (the Spokesman)

The stakeholder spends **< 4 hrs/day** on the project, so upward comms must be
**high-signal and aggregated**. A **Spokesman** service aggregates all-workstream
state from the event log and is the human interface
([ADR-0006](decisions/0006-stakeholder-comms.md)).

| Class | What | Behavior |
| --- | --- | --- |
| 🛑 **Approve (blocks)** | redefining product/workstream **objective**; requesting **additional budget** | Blocks that item; **batched into a periodic digest** (default daily) for review/discussion. |
| 📣 **Inform (non-blocking)** | major milestone; major mistake + recovery; spend change **within** approved budget | Written to the feed; work continues. |
| 🚨 **Alarm (interrupt)** | active attack, PR disaster, major security breach | **Immediate, repeats until acknowledged.** The genuine few only. |

Channels ([ADR-0006](decisions/0006-stakeholder-comms.md)): **both** a
local-hosted **dashboard** (deep, full-state console; remote via tunnel) and
**WhatsApp** (live push + quick approvals). The Spokesman posts 🛑/🚨 to WhatsApp
and maintains the dashboard; stakeholder replies re-enter the system as events/
tasks.

## 10. Cross-machine state (dev phase)

This repo is edited from a **remote session** with no access to the **execution
host**. Until the host is live, **git is the shared substrate and a low-cost
async message bus** ([ADR-0007](decisions/0007-cross-machine-state.md)):

- `state/` holds human-readable status, an append-only event snapshot, and the
  lessons corpus.
- The remote session drops instructions into a tracked **inbox**; the host polls,
  acts, and writes results/status back; both sync via commit + pull.
- Once the host runs, the **local DB is source of truth** and *exports snapshots
  to git* for the remote session.

See [`../state/README.md`](../state/README.md).

### The off-host agent (an intermittent remote worker)

Beyond the host, work can also run on an **off-host agent** — a capable agent
session on a *different* machine that shares state only through git, is **not
always available**, and holds no host secrets ([ADR-0010](decisions/0010-offhost-remote-agent.md)).
The PM uses it as a **capacity lever**: when host compute is tight, it delegates
non-urgent, host-resource-free work (research, design, docs, code drafting,
review) via `state/offhost/requests/` and collects results from
`state/offhost/results/`. The host **never blocks on it** — every delegated item
has a local fallback and a timeout.

## 11. "Assemble, don't build" — candidate components

The moat is *how we define experiments, evaluate signal, allocate resources, and
close the Builder↔Product loop* — only that top layer is written from scratch.

| Layer | Start with | Notes |
| --- | --- | --- |
| Agent runtime | OpenAI Agents SDK / PydanticAI | typed I/O, tool calling, tracing; avoid CrewAI/AutoGPT/BabyAGI |
| Orchestration | Postgres + scheduler + thin supervisor | agent-driven; **Temporal** is the upgrade path |
| Tools | MCP servers | filesystem, git, browser, postgres, … |
| Browser | Playwright | — |
| Search | gateway over Tavily/Exa/Brave | swap providers without touching agents |
| Memory | PostgreSQL + Qdrant | vector + SQL; no Neo4j yet |
| Observability | OpenTelemetry + Grafana + Prometheus | non-negotiable from day one |
| Sandbox | Docker | no agent touches the host |
| Coding worker | opencode | one replaceable Worker, not the runtime |

**opencode** is the first "employee" (a Software Engineer worker) and is fully
replaceable — the studio's brain (orchestration, memory, policy, evaluation,
business logic) stays stable as coding agents come and go. The Builder never
knows it's Claude/Gemini/opencode; it only knows "Need Prototype," and the
runtime dispatches a Worker. Study OpenHands' architecture; don't fork it.

**Skills, not prompts.** Workflows are expressed as reusable *skills* using the
Agent Skills open standard ([ADR-0008](decisions/0008-adopt-agent-skills-standard.md)) —
e.g. `Validate Startup`, `Find Competitor`, `Generate Steam Capsule`,
`Publish YouTube`, `Review Architecture` — curated from credible libraries and
reviewed like code, not ad-hoc prompts.
