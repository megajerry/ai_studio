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
inbound approve/deny resolves real approvals) · **Docker sandbox runner**
(`DockerSandboxRunner` behind `ShellTool`: network-off / non-root / read-only /
cap-drop ALL / resource+timeout / scoped-mount w/ realpath / no-secret-env; real
container host-verified) · **Task-lifecycle state machine + dependency DAG +
lifecycle telemetry** (ADR-0015 — canonical 9-state machine in `runtime/task_state.py`
+ single guarded `tasks.transition`, no ad-hoc status writes; legacy statuses
migrated `queued→up_for_grabs`/`done→merged`/`failed→abandoned` (migration 0008,
idempotent); grab-by-sort + `FOR UPDATE SKIP LOCKED`; Verifier-as-Reviewer unified
dev/review loop; prerequisite DAG `depends_on` with `ready_tasks`/`waiting_tasks`;
append-only `task_transitions` telemetry + `task_lifecycle`/`task_cost`/agent+model
rollups; docs `docs/task-lifecycle.md`) · **Real per-workstream budget enforcement**
(`runtime/budget.py` + migration 0010, idempotent: `(workstream, period)`
`cap_usd`/`cap_tokens` read against **real accrued `model.call` cost**; gated at the
single model-call site — over cap → `budget.exceeded` + a 🛑 "raise budget" approval
+ `OverBudget`, never a silent overspend; policy `BudgetContext` now token+USD on
real spend; dry-run accrues too) · onboarding/secrets · model shortlist +
cost model · ADRs 0001–0015.

## 🔄 In progress

- _(nothing in flight)_

## 📋 Remaining — buildable now (no stakeholder input needed)

1. **DB-outage resilience + remote host-restricted DB access** (part 2 of the
   lifecycle milestone) — degraded-mode contract, reconnect grace window (avoid
   thundering-herd re-kick), git fallback; Postgres LAN bind + `pg_hba` allowlist
   of authorized hosts (not internet).
2. **Experiment primitive** (venture-studio brain, first object) — an `experiment`
   (hypothesis, success metric, budget, kill/scale decision) + one evaluation step.
   Generic machinery; the *first real* experiment needs a product decision (below).
3. **Coding-worker dispatch** — route a "Need Prototype" coding task through the
   (done) sandbox runner via `invoke` (opencode as the replaceable worker).
4. **Workstream-bootstrap primitive** (makes starting a vertical config-not-code) —
   a workstream config/registration (name/objective/budget/policy grants/tool+skill
   set/memory-seed/DB-scope/object-store bucket) + the **role prompt-assembly layer**
   (shared role base + workstream charter + per-role overlay + skills + lessons +
   task) + a **pluggable verify-checker registry** (structured criterion → domain
   check, e.g. `video_audit`) so verticals augment verification while the learning/
   retro/reviewer/telemetry all still apply. Captured by the vertical-isolation ADR.
5. **Vertical-isolation ADR** — ratify: state→DB, artifacts→object store,
   product→own repo, definition→platform (this repo).
6. **Model sourcing agent** — researches models (LMArena/pricing) and proposes
   registry updates via the normal PR loop (ADR-0005).
7. **Adaptive orchestration intensity** — generalize scaling of review/retro/
   research by recent error rate + budget/telemetry (today: on_fail/on_risk).
8. **Event-type constant consolidation** — deferred nit (many `EVENT_*` strings vs
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
