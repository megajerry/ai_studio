# 0026 — Spokesman conversational interface (full human↔studio loop)

- **Status:** Accepted
- **Date:** 2026-07-26
- **Amends:** [ADR-0006](0006-stakeholder-comms.md)

## Context

[ADR-0006](0006-stakeholder-comms.md) established the Spokesman as the single
human interface and the 🛑 / 📣 / 🚨 outbound taxonomy. Inbound was described as
"stakeholder replies re-enter the system as events/tasks," but the
implementation only ever shipped keyword shortcuts (`status`, `approve` /
`deny`, `decide`). That underspecifies the role: the stakeholder's intent is
that Spokesman **knows the studio, answers in natural language, and routes
requirements into execution** — not that chat is a thin command CLI.

Outbound interrupt hygiene (ADR-0006) and verify-or-refuse grounding
([ADR-0021](0021-spokesman-grounding-accountability.md)) remain in force. This
ADR fills the **inbound conversational** half without violating CLAUDE.md
invariant 1 (agents don't call agents — coordination is via the task queue /
event log).

## Decision

### 1. Role

The Spokesman is the **default sole interface** between the human and the entire
studio. It:

- answers questions itself from grounded studio state (sync when possible);
- understands requirements / feedback and enqueues work for other roles;
- anticipates likely questions by refreshing a **prep cache** (latency aid;
  live DB remains the source of truth for grounding);
- only lets another role address the human after an **explicit handoff
  approval**, and still **relays** those messages (tagged `[Role]`) — no raw
  channel credentials for other agents.

### 2. Inbound intents

The **model interprets free text first** (agent loop via ``call_model``). Studio
actions are tools the agent may invoke — they never replace understanding the
prompt:

| Path | Behavior |
| --- | --- |
| Keyword shortcuts | Fast path only: ``status`` / ``approve`` / ``deny`` / ``decide``. |
| Free text | Spokesman agent: natural reply; optional tools ``studio_status``, ``enqueue_goal``, ``request_prep``, ``propose_handoff``, ``end_handoff``. |

Unrecognized free text is **never silently dropped** and is **never answered by
pasting a status dump under the user's words**.

### 3. Coordination with PM and others

- **PM is queue-driven:** the shared worker claims `pm.tick` / `replan` (see
  [ADR-0009](0009-agent-lifecycle-and-genesis.md)). Spokesman **never** calls
  `run_pm_tick` directly.
- Prep / clarification for Spokesman's own answers uses `spokesman.prep`
  tasks (high priority); results land in the prep cache and surface as a
  human follow-up via the existing notifier / poll path.
- Emit body-free `human.message` / `human.goal` / `spokesman.prep_ready` /
  `handoff.*` events (ids / kinds / roles only — text lives in local tables or
  channel logs, invariants 5 & 6).

### 4. Channels

SMS / WhatsApp / web chat share the same converse path. One human thread;
Spokesman voice by default; approved handoffs prefix `[PM]` (etc.) on the same
channel.

## Consequences

- Web/SMS chat becomes a real studio interface, not a command cheat-sheet.
- PM receives stakeholder goals as `pm.tick` payloads with `goal` set.
- Grounding (ADR-0021) applies to conversational answers and handoff relays.
- Keyword shortcuts remain for low-latency approve/deny/decide/status.

## Out of scope

- Giving other agents raw messaging credentials.
- Replacing the outbound 🛑/📣/🚨 classifier.
- Full multi-turn memory beyond prep cache + event log (ADR-0013 follow-up).
