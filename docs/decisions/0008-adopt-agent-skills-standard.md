# 0008 — Adopt the Agent Skills open standard for roles & reusable capabilities

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

We want roles and reusable workflows packaged as portable, reviewable units, not
as ad-hoc prompt strings ("skills, not prompts"). "Agent Skills" — a `SKILL.md`
(YAML frontmatter + instructions + optional scripts/templates) loaded on demand —
originated at Anthropic, was released as an **open standard**, and is being adopted
by a growing set of agent tools (e.g. Claude Code, opencode, Codex, Gemini CLI,
Cursor — verify current per-tool support before relying on it). Curated libraries
exist (`anthropics/skills`, `VoltAgent/awesome-agent-skills`), including a PM
suite to study (`alirezarezvani/claude-skills`).

## Decision

Package role know-how and reusable workflows as **Agent Skills** using the open
standard, so they are portable across the tools we assemble (notably opencode as
a Worker). A role is therefore `prompt + skills + tools`:

- **prompt** = the role persona / operating instructions;
- **skills** = on-demand capability packages (SKILL.md);
- **tools** = capability-gated actions.

We will **curate before build**: pull credible existing skills (PM,
retrospective, review) from the ecosystem, and **treat skills like code — review
every skill before use, prefer audited sources.** The Researcher role owns
sourcing and adapting skills.

## Consequences

- Skills are portable to any skill-aware runtime/Worker → reinforces "assemble,
  don't build" and opencode's replaceability.
- We need a review gate for imported skills (supply-chain risk).
- Studio workflows (`Validate Startup`, `Find Competitor`, `Generate Steam
  Capsule`, `Review Architecture`, …) become skills over time.

## References

- agentskills/agentskills (open standard, Apache-2.0); anthropics/skills;
  VoltAgent/awesome-agent-skills; alirezarezvani/claude-skills (PM suite).
- Safety guidance ("treat skills like code; review before use") —
  skillmatic-ai/awesome-agent-skills.
