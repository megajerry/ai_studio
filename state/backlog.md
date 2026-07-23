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
wiring** · **Docker sandbox runner** · **Task-lifecycle state machine + dependency
DAG + telemetry** (ADR-0015; migration 0008) · **Real per-workstream budget
enforcement** (migration 0010) · **DB-outage resilience + remote host-restricted
access** (ADR-0017) · **Experiment primitive** (ADR-0016; migration 0009) ·
**Coding-worker dispatch** (opencode inside the sandbox; `code.run` 🔴) ·
**Role-customization seams** (`runtime/roles/prompt.py` `compose_role_prompt`
[base→charter→overlay→skills→lessons→task, behavior-preserving] + `roles/checkers.py`
pluggable verify-checker registry [structured criterion, marker back-compat,
evidence-based, e.g. `video_audit`]) · **Workstream config/registration** (a
vertical is config-not-code: `runtime/workstream/` `WorkstreamConfig` from
`workstreams/<name>/config.yaml` [charter/overlays/budget/policy-grants/skills/
checkers/memory-seed/bucket] drives the runtime seams via minimal worker wiring,
behavior-preserving with no config, scope-isolated; `bootstrap_workstream`
idempotently seeds memory+budget; ADR-0018 vertical isolation) · **Cross-workstream
request contract** (the other half of workstream-bootstrap: a typed
`feature_request` on the receiver's task board + receiving-PM
intake/triage/decompose/decline/clarify + symmetric 🛑 escalation; verticals
coordinate via the board/event log, never direct calls; events leak no bodies;
`runtime/crossworkstream.py` + `roles/pm.py` `triage_request`; `docs/cross-workstream.md`)
· onboarding/secrets · model shortlist + cost model · ADRs 0001–0018.

## 🔄 In progress

- _(nothing in flight)_

## 📋 Remaining — buildable now (no stakeholder input needed)

1. **Model sourcing agent** — researches models (LMArena/pricing) and proposes
   registry updates via the normal PR loop (ADR-0005).
2. **Adaptive orchestration intensity** — generalize scaling of review/retro/
   research by recent error rate + budget/telemetry (today: on_fail/on_risk).
3. **Event-type constant consolidation** — deferred nit (many `EVENT_*` strings vs
   M1's `EventType` enum). Do last / alone (touches many modules). _(Now also
   includes the `request.*` wires in `runtime/crossworkstream.py`.)_

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
- Budget pre-call USD estimate uses input-only pricing (conservative under-count).
- `call.py` budget step-comment numbering is off-by-one (cosmetic).

## Context (from the PM audit, 2026-07-22)

Verdict was **AT-RISK**: the *platform substrate* is A-grade and verified, but the
*venture-studio value layer* had not begun and the PM was a stub. The real-PM +
experiment-primitive + role-customization-seam milestones close most of that gap;
a first real vertical (needs a product decision) remains the path to an actual
venture studio. Everything is still **dry-run/keyless** until go-live keys.
