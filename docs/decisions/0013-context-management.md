# 0013 — Context management for long-running agents

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

Per-task cost is dominated by **context size**, and in agentic loops context
**grows every step** ([cost-model.md §1](../cost-model.md)). A long-running agent
that never manages its context suffers runaway cost *and* degraded quality
(signal buried in bloat). The PM session that bootstrapped this repo was a live
example: one monolithic thread carrying 350k+ tokens, re-billed every turn.

The stakeholder's guidance: **don't force exact rules** on when/how to compact —
but every agent must be *aware* of this nature and act accordingly.

## Decision

All agents are built to treat context as the primary cost/quality lever and
**manage it deliberately** (the *when/how* is left to the agent + runtime +
budget/telemetry, not a hard-coded threshold):

1. **Scope, don't accumulate.** Read only the task's relevant context (per the
   four-layer memory); don't drag along a monolithic history.
2. **Compact when it grows.** Summarize completed steps / drop stale tool output
   / keep a tight working set. Prefer **starting a fresh small-context subagent**
   over growing one long thread.
3. **Concise reasoning, complete facts.** Be terse in *reasoning and prose*, but
   **never omit verifiable data or critical details** — IDs, exact values, file
   paths, decisions, acceptance criteria, error messages, commands. Concision
   applies to how you explain, not to what's true and checkable.
4. **Budget-aware.** Let remaining budget / observed context size inform how
   aggressively to compact (telemetry, [ADR-0012](0012-telemetry-metrics.md)).

## Consequences

- Role prompt/skill templates must carry this instruction.
- The runtime should support **compaction checkpoints** (summaries persisted to
  the event log / memory so a compacted or freshly-spawned agent can resume with
  full facts but a small context).
- Telemetry tracks context size per task, which can *trigger* compaction and flag
  bloated tasks for the Retro role.
- Compaction must be **lossless on facts**: a summary that drops a verifiable
  detail is a bug, not a saving.
