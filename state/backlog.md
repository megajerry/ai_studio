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
Whistle-blower role · **Researcher role** · WhatsApp Spokesman + **Spokesman↔runtime
wiring** · **Docker sandbox runner** (network-off/non-root/read-only/cap-drop/
resource+timeout/scoped-mount/no-secret-env) · **Task-lifecycle state machine +
dependency DAG + telemetry** (ADR-0015: 9-state guarded machine, grab-by-sort,
Verifier-as-Reviewer, prereq DAG, task_transitions + rollups; migration 0008) ·
**Real per-workstream budget enforcement** (migration 0010: real accrued-spend
caps → over-cap = `budget.exceeded` + 🛑 + `OverBudget`, no silent overspend) ·
**DB-outage resilience + remote host-restricted access** (ADR-0017:
`connect_with_retry`/`DBUnavailable` degraded mode; supervisor reconnect grace
window; loopback-default bind + env `pg_hba` scram allowlist, never trust/`0.0.0.0/0`)
· **Experiment primitive** (ADR-0016: `runtime/experiment/`; hypothesis + metric +
budget + guarded `proposed→running→evaluated→kept|scaled|killed`; evidence-based
kill/scale — over-budget/missed→killed, met→kept, strongly-met→scaled + 🛑;
migration 0009) · **Coding-worker dispatch** (architecture §14: `work.code`/
`prototype` → policy-gated `coding` tool → opencode INSIDE the sandbox; `code.run`
🔴/approval-gated via `invoke`; opencode swappable via `CODING_WORKER_CMD`;
loop-free worker path; env-allowlist-only, no host secrets) · onboarding/secrets ·
model shortlist + cost model · ADRs 0001–0017.

## 🔄 In progress

- _(nothing in flight)_

## 📋 Remaining — buildable now (no stakeholder input needed)

1. **Workstream-bootstrap primitive** (makes starting a vertical config-not-code) —
   a workstream config/registration (name/objective/budget/policy grants/tool+skill
   set/memory-seed/DB-scope/object-store bucket) + the **role prompt-assembly layer**
   (shared role base + workstream charter + per-role overlay + skills + lessons +
   task) + a **pluggable verify-checker registry** (structured criterion → domain
   check, e.g. `video_audit`) + the **cross-workstream request contract** (typed
   `feature_request` + receiving-PM intake/triage/prioritize/approve/decompose +
   symmetric escalation). Captured by the vertical-isolation ADR.
2. **Vertical-isolation ADR** — ratify: state→DB, artifacts→object store,
   product→own repo, definition→platform (this repo).
3. **Model sourcing agent** — researches models (LMArena/pricing) and proposes
   registry updates via the normal PR loop (ADR-0005).
4. **Adaptive orchestration intensity** — generalize scaling of review/retro/
   research by recent error rate + budget/telemetry (today: on_fail/on_risk).
5. **Event-type constant consolidation** — deferred nit (many `EVENT_*` strings vs
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
- Budget pre-call USD estimate uses input-only pricing (conservative under-count;
  real accrued spend dominates) — tighten if strict USD conservatism wanted.
- `call.py` budget step-comment numbering is off-by-one (cosmetic).

## Context (from the PM audit, 2026-07-22)

Verdict was **AT-RISK**: the *platform substrate* is A-grade and verified, but the
*venture-studio value layer* had not begun and the PM was a stub. The real-PM +
experiment-primitive milestones close that gap; a first real vertical remains the
path to an actual venture studio. Everything is still **dry-run/keyless** until
go-live keys are provided.
