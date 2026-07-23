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
**Role-customization seams** (prompt-assembly layer + pluggable verify-checker
registry, e.g. `video_audit`) · **Workstream config/registration** (a vertical is
config-not-code; `runtime/workstream/`; ADR-0018 vertical isolation) ·
**Cross-workstream request contract** (typed `feature_request` + receiving-PM
intake/triage/decompose/decline/clarify + symmetric 🛑 escalation; coordinate via
the board, never direct calls; `runtime/crossworkstream.py` + `roles/pm.py`
`triage_request`) · **Adaptive orchestration intensity** (ADR-0003:
`runtime/adaptive.py` scales review/retro/research by real recent error rate +
budget headroom; evidence-based, bounded, off by default → behavior-preserving) ·
**Model sourcing agent** (ADR-0005: `runtime/roles/sourcing.py` researches
models/pricing via the policy-gated search gateway → proposes a reviewable candidate
registry update + the approval envelope 🛑 provider/budget / auto+📣 in-band;
evidence-grounded provenance; never mutates the live registry; no loop) ·
**Event-type constants consolidated** (single `runtime/event_types.py`, pure
refactor, zero wire-value drift) · **Critic role + PM↔Critic consensus loop**
(ADR-0019: `runtime/roles/critic.py` — forward adversarial partner returning
risk/downside/missed_opportunity/alternative concerns + proceed/revise/escalate;
BOUNDED consult↔revise loop in `roles/pm.py` that decomposes on consensus or
escalates a genuine disagreement 🛑; opt-in + behavior-preserving; distinct from the
after-the-fact Reviewer; `critic.reviewed`/`pm.consensus` leak no bodies) ·
**Evaluation harness v1** (empirical quality framework: coverage wired via
`pytest-cov`/`.coveragerc`/`make coverage`; seeded-defect **Verifier
precision/recall** = 1.0/1.0 on a labeled GOOD/BAD corpus incl. hallucinated-success
+ `video_audit` defects; **PM structural decomposition** eval; telemetry-driven
**`quality_report`** in `runtime/quality.py`; `python -m evals`; `docs/evaluation.md`) ·
onboarding/secrets · model shortlist + cost model · ADRs 0001–0019.

## 🔄 In progress

- _(nothing in flight)_

## 📋 Remaining — buildable now (no stakeholder input needed)

- **✅ NONE — the buildable backlog is exhausted.** Everything achievable without
  stakeholder input (keys / budget / product-scope / provisioning) is built,
  reviewed under evidence rigor, and merged. Only the boundary items below remain.

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
- **Real-model evals + real-integration smoke** — the go-live half of the
  evaluation harness: real-model golden-set + LLM-as-judge OUTCOME evals, and
  end-to-end smoke against Docker/Qdrant/live providers/WhatsApp. The harness
  framework, seams, and report shape already exist (`evals/`, `runtime/quality.py`,
  `docs/evaluation.md`); this item is the real-model/real-integration content that
  only becomes measurable once keys land.

## 🐛 Known follow-up nits (tracked, non-blocking)

- Recall relevance floor (`min_score`) default for lesson injection before real
  embeddings land.
- `find_grant`/read-path commit coupling on non-autocommit connections (harmless).
- `state/status.md` should keep an evidence-based (command + count) status line.
- Budget pre-call USD estimate uses input-only pricing (conservative under-count).
- `call.py` budget step-comment numbering is off-by-one (cosmetic).
- `effective_policy` REPLACE-not-union for a workstream's role grants (documented).

## Context (from the PM audit, 2026-07-22)

Verdict was **AT-RISK**: the *platform substrate* is A-grade and verified, but the
*venture-studio value layer* had not begun and the PM was a stub. The real-PM,
experiment-primitive, role-customization-seam, and workstream-bootstrap milestones
close that gap; a **first real vertical** (needs a product decision) remains the
path to an actual venture studio. Everything is still **dry-run/keyless** until
go-live keys.
