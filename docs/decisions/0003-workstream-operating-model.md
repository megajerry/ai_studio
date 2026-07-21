# 0003 — Workstream operating model & roles

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

Mainstream models show three recurring failure modes: (a) stopping prematurely
when a nudge would finish the task, (b) executing before understanding it, and
(c) not learning from mistakes over time. We need an operating model that fixes
these structurally rather than hoping the model behaves.

The supervisor/planner pattern is the dominant production shape in 2026, and the
reflection/Reflexion literature provides a validated basis for self-improvement.

## Decision

A workstream is a set of **atomic roles**, each `= prompt + skills + tools`,
coordinated by adaptive orchestration ([ADR-0004](0004-agent-driven-orchestration.md)):

- **PM** — the supervisor. Owns completion; runs a **confidence gate** before
  execution (restate requirement → success criteria → plan → self-score); may
  **push back** on unreasonable requirements as a first-class output. Gets the
  **highest-quality model** (best practice: match models to roles).
- **Executor(s)** — do the domain work through tools.
- **Reviewer / Whistle-blower** — independent guard against failure/disaster.
- **Retro** — after episodes, distills durable **lessons** into the Knowledge
  memory layer, **auto-injected at prompt-assembly time** into future work.
- **Researcher** — mines external best-practice/tools/skills into reusable assets.

Properties:

- **Completion is verified by an independent agent**, not self-asserted (fixes a).
- **Adaptive intensity** — review/retro/research run async, scaled by recent
  error rate, domain velocity, and token/time budget (not a fixed cycle).
- **Roles never call each other directly** — coordination is via the event log /
  queue only.

Design explicitly against the documented failure modes:

- **Handoff loops** (the #1 multi-agent failure) → single task owner + event-driven
  coordination, no free-form agent-to-agent handoff.
- **Hallucination cascade** → independent verifier before any commit.
- **Diminishing reflection returns** → cap reflection to ~2 iterations; prefer
  **prompt-level prevention** (durable lessons) over runtime correction, since
  cross-episode accumulation dominates single-pass gains.
- **Supervisor context cost** → budget the PM's context explicitly; token spend
  concentrates there.

## Consequences

- Roles are reusable templates the Productivity platform provides
  ([ADR-0002](0002-productivity-horizontal-platform.md)); skills are packaged per
  [ADR-0008](0008-adopt-agent-skills-standard.md).
- PM quality is the highest-leverage investment.
- The lessons corpus + injection path must exist for (c) to be solved.

## References

- Multi-agent orchestration patterns (supervisor dominant; match models to roles;
  handoff loops / hallucination cascade as top failure modes), 2026 surveys:
  digitalapplied.com, thinking.inc, beam.ai.
- Reflexion / RetroAct / Multi-Agent Reflexion — retrospective lessons, dual-role
  critic, diminishing single-pass returns: promptingguide.ai/techniques/reflexion,
  arxiv 2405.10467, 2503.01490.
