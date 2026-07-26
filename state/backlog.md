# AI Studio — backlog (single source of truth)

_Maintained by the Productivity workstream. Updated as items land. Milestone
detail + evidence live in `git log` and `state/status.md`; this file is the
"what's left" list._

_Last updated: 2026-07-23._

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
**Evaluation harness v2** (empirical quality framework, now statistically honest:
**corpus-as-data** `evals/corpus/*.yaml` (7 verifier + 3 PM cases, all v1 coverage
preserved); **CI-aware metrics** — every rate carries `n` + Wilson 95% CI +
`INSUFFICIENT(n<30)` flag, so a 1.0 on tiny n reads as `[0.566,1.0] INSUFFICIENT`,
not a trustworthy number; **swappable LLM-judge** (`evals/judge.py` via
`runtime.model.call` — dry-run today, real model at go-live with zero code change,
proven by a replay cassette); **record/replay** VCR (`evals/replay.py`);
**trajectory-level eval** scoring a persisted PM trajectory against a rubric;
coverage wired (`make coverage`); telemetry `quality_report`; `python -m evals`;
`docs/evaluation.md` corrects the v1 tiny-n overclaim) ·
**Trajectory observability — full stack** (ADR-0020; migration 0011:
`trajectories` + `trajectory_steps` + `tasks.trajectory_id`): single guarded writer
`runtime/trajectory.py` (gapless per-trajectory `seq`, injectable `now`, body-free
`trajectory.*` events, verbatim→lean rotation + TTL expiry) · **PM + Critic emit
reasoning trajectories** (`roles/pm.py`/`roles/critic.py`: ordered
observe→plan→decide→consult→revise→decompose→escalate→commit steps w/ verbatim
rationale + confidence; decomposed tasks stamped w/ `trajectory_id`; DB-outage-safe,
behavior-preserving) · **outcome attribution + CI-aware PM decision-quality metrics**
(`runtime/quality.py`: joins trajectory→tasks→lifecycle outcomes → first-pass-merge
/rework/escalation/abandoned rates, each w/ `n` + Wilson CI + insufficient-sample
flag) · **live-session ingest bridge** (`runtime/trajectory_ingest.py` + CLI —
ingests an off-host/uninstrumented agent's trajectory via the guarded writer so the
running session is measurable) · **learning-agent rotation/TTL worker**
(`runtime/trajectory_worker.py` + launchd plist: TTL expiry + opt-in verbatim→lean
rotation of Retro-mined trajectories) ·
**Spokesman grounding & accountability** (ADR-0021; migration 0012): everything
told to the human must be grounded — `Claim`/`EvidenceRef` contract
(`runtime/grounding.py`); the Spokesman `/notify` is now a **verify-or-refuse gate**
(`spokesman/grounding_gate.py`) that structurally verifies each claim's evidence
against source of truth → VERIFIED relayed / UNVERIFIABLE withheld + `comms.proof_requested`
/ contradicted = **fabrication**; **zero-tolerance penalty** (`runtime/trust.py`
ledger): one fabrication → permanent human-relay revocation + quarantine from
`grab_task` + strike + 🚨 escalation + verifier-chain cascade; body-free `comms.*`/`trust.*`
events; `COMMS_HUMAN_RELAY` capability; **fabrication-rate telemetry** (`quality.py`,
n + Wilson CI) + measurability eval. Adversarially reviewed (26+ attacks, all caught) ·
**Capacity-governance foundation** (ADR-0022; migration 0013): deterministic
graduated budget zones (warn→throttle→**reserve**→hard) in `runtime/budget.py` with
a reserve buffer spendable only on `wind_down`/`escalation` (so a workstream keeps
tokens to react/escalate before breach), two-level org+allocation ceiling,
burn-rate projection; body-free `budget.warn/throttle/reserve` events; additive
(NULL fracs = old hard cap); **optional Capacity Steward role** (C2,
`runtime/roles/capacity_steward.py`, config-not-code, OFF by default — flags/recommends,
never enforces or raises ceilings) + **budget-aware role charters** (compact→wind-down→
escalate before breach) + **`purpose` threading** (`call_model(purpose=…)` → reserve zone
permits wind_down/escalation); **capacity telemetry** (C3, `quality.py` `capacity_report`:
per-workstream zone/burn/projected-breach + studio roll-up w/ Wilson-CI at-risk rate) + eval ·
**Cursor coding substrate + guarded router adapter** (`runtime/model/providers/cursor_cli.py`:
inference only via the agent-harness CLI `cursor-agent -p --output-format json` — NO raw
HTTP endpoint; hard timeout + auto-fallback to Opus for the hang bug; **`agentic` task-type
only**, opt-in via `CURSOR_API_KEY`, never on cheap/classify/embed tiers; `code.run` stays
🔴; Ultra $200/mo in cost model; `cursor-agent` also a swappable coding worker) ·
**Self-healing recovery ladder** (ADR-0023; migration 0014) — replaces the supervisor's
binary re-kick/abandon with a graduated, progress-aware ladder: **nudge+grace** (cheap;
transient stalls recover without discarding progress) → **progress-aware re-kick** →
**early `task.stuck` escalation** (bails at a no-progress threshold *before* exhausting
retries, so no reset-forever loop) → **PM re-decomposition** (`run_pm_replan`: stuck
monolith → smaller DAG subtasks, bounded replan depth → 🛑) → abandon backstop; plus
**failure-reason capture** (`model.call.failed` error class) → **failure-pattern detector**
(`roles/failure_analyst.py`: fires only at n≥floor AND Wilson-lower-bound>threshold →
proposes a reviewable durable fix, never auto-applies) → **verify-as-experiment** on real
post-fix traffic; all body-free events, supervisor stays non-LLM ·
**Skill induction + dual-source learning** (ADR-0024; research-first, prior-art-grounded —
Voyager/AWM/Agent-Skills/DreamCoder/DSPy/LATM): **P0** `skill.applied` attribution (body-free)
· **P1** `quality.skill_efficacy_report` (applied-vs-baseline on iterations/tokens/tool+search
calls/first-pass, task_type-family pooled, n + Wilson CI) — validated on existing skills first
· **P2** Skill **Curator** (`roles/curator.py`: induces recurring+mature+efficient trajectory
clusters → proposes a `reviewed:false` candidate; never mutates live) · **P3** dual-source
convergence (`skills/review_queue.py`: one queue over Curator + Researcher candidates + Retro→
Curator handoff) + **human-gated 🔴 promote** (adopt to live `skills/` `reviewed:true` only on
an approved one-shot grant) · **P4** keep/tune/retire (`roles/skill_lifecycle.py`: applied-cohort
efficacy verdict → reviewable deprecation/revision proposal, never auto-retires). Dual-source
(Retro internal + Researcher external); statistically gated (n≥floor + Wilson bound); adoption
= review-gate (narrow auto-adopt lane deferred to its own ADR); **P5 hierarchy/skill-trees
DEFERRED** (intra-skill progressive disclosure adopted) ·
onboarding/secrets · model shortlist + cost model · ADRs 0001–0024.

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
  evaluation harness. The *mechanism* now fully exists keyless (eval-v2): swappable
  LLM-judge (dry-run→real, zero code change), record/replay cassettes, CI-aware
  metrics, corpus-as-data, trajectory-level scoring, and live-session trajectory
  ingest. What remains needs keys/providers: a larger real-model golden-set + the
  real LLM-as-judge OUTCOME numbers (with CIs), and end-to-end smoke against
  Docker/Qdrant/live providers/WhatsApp. Only the real-model *content* is pending,
  not the harness.

## 🐛 Known follow-up nits (tracked, non-blocking)

- Recall relevance floor (`min_score`) default for lesson injection before real
  embeddings land.
- `find_grant`/read-path commit coupling on non-autocommit connections (harmless).
- `state/status.md` should keep an evidence-based (command + count) status line.
- Budget pre-call USD estimate uses input-only pricing (conservative under-count).
- `call.py` budget step-comment numbering is off-by-one (cosmetic).
- `effective_policy` REPLACE-not-union for a workstream's role grants (documented).
- Eval-harness auto-seeds throwaway `eval-traj-*` trajectory rows each run
  (isolated, not in the telemetry rollup); wants periodic cleanup if run often.
- Trajectory-writer in-repo concurrency test is sequential-interleaved (the
  mechanism was proven race-free out-of-band with an 8-thread load); wants a
  threaded regression test.
- Ingest CLI could scrub/warn on non-synthetic example files (invariant documented;
  shipped example is synthetic).
- ✅ **RESOLVED (db70e39)** — Grounding gate false-positive fabrication risk: a
  malformed `expected` now → UNVERIFIABLE (proof requested, no strike), reserving
  fabrication for a well-formed `expected` that genuinely contradicts source of truth
  (per-`EvidenceKind` validation in `spokesman/grounding_gate.py`). Minor open
  follow-up: within a SINGLE `db_row` ref, a malformed column short-circuits before a
  genuine contradiction on another field → UNVERIFIABLE (misses the strike) but still
  WITHHOLDS the false claim (fails safe); optionally make a real contradiction take
  precedence over a malformed sibling field within one ref.
- ✅ **RESOLVED (b867a62)** — Test isolation under a polluted shared DB: the suite
  already tolerates a polluted queue via per-test unique-workstream scoping (verified
  888 passing under 5280 stray `up_for_grabs`); 2 genuinely concurrency-flaky
  `runtime_bridge` tests were hardened to own-scope invariants. An unconditional
  session-TRUNCATE fixture was evaluated and REJECTED as net-negative on the shared
  multi-agent DB (clobbers concurrent runs' scoped rows + ACCESS-EXCLUSIVE startup
  stall). Real per-session schema/DB isolation remains a possible future upgrade if
  concurrent test runs ever need full independence.
- `burn_rate` per-minute rate can inflate when seeded `model.call` events share a
  near-zero timestamp span (guard only covers <2 calls); non-blocking, `calls_to_exhaustion` unaffected. (ADR-0022 polish.)
- Budget pre-spend check runs against the *routed* spec; a Cursor→Opus runtime
  fallback call serves on metered Opus but isn't pre-gated for that one call (cost
  is accounted post-hoc, so spend catches up next call). Minor edge on the rare
  hang-fallback path. (Cursor/ADR-0022 awareness.)
- `test_quality_capacity_db.py` / some capacity tests seed prefixed rows with no
  teardown (rely on unique-prefix isolation) → orphan rows accumulate in the shared
  DB across runs. Add a fixture `finally` deleting `LIKE '<pfx>%'`. (Test-hygiene.)

## Context (from the PM audit, 2026-07-22)

Verdict was **AT-RISK**: the *platform substrate* is A-grade and verified, but the
*venture-studio value layer* had not begun and the PM was a stub. The real-PM,
experiment-primitive, role-customization-seam, and workstream-bootstrap milestones
close that gap; a **first real vertical** (needs a product decision) remains the
path to an actual venture studio. Everything is still **dry-run/keyless** until
go-live keys.
