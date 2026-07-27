# Architecture Decision Records

Every architecture-affecting decision is recorded here as an ADR (the practice
itself is [ADR-0001](0001-record-architecture-decisions.md)). The
architecture-of-record ([`../architecture.md`](../architecture.md)) links into
these for detail; this file is the index.

| # | Decision | One-line summary |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Use lightweight, numbered ADRs for any architecture-affecting decision. |
| [0002](0002-productivity-horizontal-platform.md) | Productivity is the horizontal platform | This repo is the one horizontal workstream every vertical draws on. |
| [0003](0003-workstream-operating-model.md) | Workstream operating model & roles | Atomic roles (`prompt + skills + tools`) under adaptive orchestration; fixes named failure modes. |
| [0004](0004-agent-driven-orchestration.md) | Agent-driven orchestration + minimal supervisor | PM plans/schedules; one non-agent supervisor guarantees no task is silently dropped; Temporal is the upgrade path. |
| [0005](0005-model-registry-router.md) | Model registry, router & sourcing | Versioned model catalog + data-not-code routing policy + sourcing agent via the PR/review loop. |
| [0006](0006-stakeholder-comms.md) | Stakeholder comms taxonomy & Spokesman | 🛑 approve / 📣 inform / 🚨 alarm classes; the Spokesman is the single human interface. |
| [0007](0007-cross-machine-state.md) | Cross-machine state via a git substrate | Until the host is live, `state/` in git is the shared substrate + async message bus. |
| [0008](0008-adopt-agent-skills-standard.md) | Adopt the Agent Skills open standard | Workflows are reusable skills, curated and reviewed like code before use. |
| [0009](0009-agent-lifecycle-and-genesis.md) | Agent lifecycle & genesis | A few ensured singletons; every role agent is spawned on-demand per task and torn down. |
| [0010](0010-offhost-remote-agent.md) | Off-host remote agent | An intermittent remote worker that shares state only via git and holds no host secrets; host never blocks on it. |
| [0011](0011-secrets-and-onboarding.md) | Secrets, credentials & cold-start onboarding | Secrets never reach an agent; real values collected into a git-ignored local file. |
| [0012](0012-telemetry-metrics.md) | Telemetry & metrics as a product requirement | What to capture (cost, tokens, latency, reliability, routing, budget, trajectory) from day one. |
| [0013](0013-context-management.md) | Context management for long-running agents | Scope context to the task; compact as it grows; prefer fresh small-context subagents. |
| [0014](0014-validation-rigor.md) | Validators trust evidence, not claims | Validation rests on hard evidence; rates carry `n` + a confidence interval. |
| [0015](0015-task-lifecycle-state-machine.md) | Canonical task-lifecycle state machine | One DB-free state machine + a single guard (`runtime.tasks.transition`); no ad-hoc status writes. |
| [0016](0016-experiment-primitive.md) | The experiment primitive | Hypothesis + target metric confirmed/denied from real-traffic facts — the venture-studio brain's first object. |
| [0017](0017-db-resilience-and-remote-access.md) | DB-outage resilience & remote access | Degrade rather than crash during a Postgres outage; remote DB access is host-restricted by an allowlist. |
| [0018](0018-vertical-isolation.md) | Vertical isolation | Verticals are config-not-code; each vertical's four kinds of state are isolated by construction. |
| [0019](0019-critic-role-and-consensus.md) | Critic role & PM↔Critic consensus | A forward-looking adversarial partner challenges decisions *before* commit; bounded consensus loop. |
| [0020](0020-trajectory-observability.md) | Trajectory observability | Persist the reasoning trajectory (how a decision was reached) as first-class, replayable, outcome-linkable data. |
| [0021](0021-spokesman-grounding-accountability.md) | Spokesman grounding + accountability | Verify-or-refuse gate on every human-facing claim; zero-tolerance fabrication penalty via the trust ledger. |
| [0022](0022-capacity-governance.md) | Graduated capacity governance | Budget zones (warn → throttle → reserve → hard-stop) + reserve buffer + burn-rate projection; optional Capacity Steward. |
| [0023](0023-graduated-recovery.md) | Graduated, progress-aware recovery ladder | Supervisor recovery ladder (nudge → re-kick → escalate → re-decompose → abandon) + a failure-pattern analyst. |
| [0024](0024-skill-induction.md) | Skill induction | Measure-first efficacy + a review-gated Curator that induces reusable skills from recurring, mature trajectories. |
| [0025](0025-async-decisions.md) | Async open-ended decisions | Park → free worker → resume on free-text/option answers (open-ended sibling of approvals). |
| [0026](0026-spokesman-conversational-interface.md) | Spokesman conversational interface | Full human↔studio NL loop; goals → `pm.tick` via queue; prep cache; approval-gated handoffs. |
| [0027](0027-build-vs-buy-agile-adoption.md) | Build vs. buy/borrow + agile adoption | PM operating principle (not a cron): weigh building in-house vs adopting a mature component; stay agile on better paradigms without churn. |
| [0028](0028-remote-task-access-gateway.md) | Non-LAN remote task access | Remote sessions get a scoped, token-gated task-verb API over the tunnel — never a DB credential; Postgres stays loopback/LAN-only. |
| [0029](0029-disposable-db-test-guard.md) | Keep the live DB sacred | DB-backed tests skip unless the target DB is disposable (`AI_STUDIO_TEST_DB` opt-in or `*_test` name); `runtime.demo` self-cleans its own workstreams. |
| [0030](0030-prod-vs-test-traffic-tag.md) | Prod vs test traffic tag | Every enqueue carries `payload.traffic` (`prod`/`test`); never infer test-ness from goal text. |
| [0031](0031-role-agnostic-dispatcher.md) | Role-agnostic dispatcher + PM commissions roles | Worker dispatch is a single `task_type→handler` registry; PM enqueues any role by judgment; Critic stays an in-process consult. |
