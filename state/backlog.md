# AI Studio — backlog (single source of truth)

_Maintained by the Productivity workstream. Updated as items land. Milestone
detail + evidence live in `git log` and `state/status.md`; this file is the
"what's left" list._

_Last updated: 2026-07-22._

## ✅ Done (merged to `main`, verified on a live Postgres unless noted)

Platform: M0 infra spine · M1 event log + task queue · M2 policy engine +
capability-gated tools · M3a supervisor + scheduler · M3b model router · M3c roles
+ worker (operating loop) · Memory (four-layer) · Search gateway · Skills ·
Learning loop (Retro → lessons → injected) · deterministic event replay (bug fix)
· concurrency/hardening · human-in-loop approvals · review-rigor doctrine
(ADR-0014) · **real PM** (understand → confidence gate → decompose) · Reviewer /
Whistle-blower role · **Researcher role** (external mining → distilled Knowledge
lessons + candidate skills `reviewed: false`) · WhatsApp Spokesman service +
**Spokesman↔runtime wiring** (event log / tasks / approvals / spend → 🛑/📣/🚨;
inbound approve/deny resolves real approvals) · onboarding/secrets · model
shortlist + cost model · ADRs 0001–0014.

## 🔄 In progress

- _(nothing in flight)_

## 📋 Remaining — buildable now (no stakeholder input needed)

1. **Experiment primitive** (venture-studio brain, first object) — an `experiment`
   (hypothesis, success metric, budget, kill/scale decision) + one evaluation step.
   Generic machinery; the *first real* experiment needs a product decision (below).
2. **Real budget enforcement** — per-workstream $/token caps that actually gate
   (today: router downshift + `OverBudget` on dry-run tokens only).
3. **opencode / Docker sandbox worker** — implement a `SandboxRunner` behind the
   `ShellTool` seam and dispatch a "Need Prototype" coding task. (Docker verified
   on host.)
4. **Model sourcing agent** — researches models (LMArena/pricing) and proposes
   registry updates via the normal PR loop (ADR-0005).
5. **Adaptive orchestration intensity** — generalize scaling of review/retro/
   research by recent error rate + budget/telemetry (today: on_fail/on_risk).
6. **Event-type constant consolidation** — deferred nit (many `EVENT_*` strings vs
   M1's `EventType` enum).

## ⛔ Boundary — needs stakeholder input (the true "exhausted" line)

- **Model provider keys + budget ceiling + executor substrate** — required to run
  anything with a real model instead of dry-run. See `docs/model-shortlist.md`,
  `docs/cost-model.md`, `state/outbox/0001-model-key-request.md`.
- **First real experiment / vertical workstream** — *what should the studio
  attempt first?* Converts "A-grade platform" into "a working venture studio".
- **WhatsApp provisioning** — Meta WhatsApp Business number + tokens + a public
  tunnel, to take the Spokesman channel live. See `docs/spokesman-whatsapp.md`.
- **Real live-model end-to-end slice** — depends on the keys above; proves a real
  model producing real work + real spend enforced (unblocks after go-live).

## 🐛 Known follow-up nits (tracked, non-blocking)

- Recall relevance floor (`min_score`) default for lesson injection before real
  embeddings land.
- `find_grant`/read-path commit coupling on non-autocommit connections (harmless).
- `state/status.md` should keep an evidence-based (command + count) status line.

## Context (from the PM audit, 2026-07-22)

Verdict was **AT-RISK**: the *platform substrate* is A-grade and verified, but the
*venture-studio value layer* had not begun and the PM was a stub. The real-PM
milestone closed the biggest gap; the experiment primitive + a first real vertical
remain the path to an actual venture studio. Everything is still **dry-run/keyless**
until go-live keys are provided.
