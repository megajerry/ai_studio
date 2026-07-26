# Studio status

_Updated: 2026-07-23. Pointers: **what's left** → [`backlog.md`](backlog.md);
**per-milestone detail + evidence** → `git log`; **design** → `docs/decisions/`._

## Phase

- **Platform complete & operating end-to-end (keyless, on a live Postgres).**
  Buildable backlog exhausted — only stakeholder-boundary items remain (below).
- Verified: **899 tests pass, 0 skips** on a real Postgres; `python -m runtime.demo`
  runs 6 green acts (operate · learn · reviewer-guard · research · config-drives-vertical · critic-consensus).

## Capabilities (one line each; details in git log / docs)

- **Runtime:** event log + task queue · canonical task state machine + dependency DAG + lifecycle telemetry (ADR-0015) · policy engine + capability-gated tools · supervisor + scheduler · model router · worker.
- **Roles:** PM (understand→gate→decompose) · Executor · Verifier · Reviewer/Whistle-blower · **Critic** (forward adversarial partner + PM↔Critic consensus, ADR-0019) · Retro · Researcher · Sourcing.
- **Depth:** four-layer Memory · Search gateway · Skills · Learning loop · human-in-loop approvals · budget enforcement · experiment primitive · coding-worker (opencode in sandbox) · DB-outage resilience + remote allowlist · Spokesman↔runtime.
- **Trajectory observability (ADR-0020):** PM+Critic reasoning persisted as first-class trajectories (`runtime/trajectory.py`, migration 0011) — ordered decision steps w/ verbatim rationale, tasks stamped w/ `trajectory_id`; **outcome attribution** (trajectory→tasks→outcomes) + **CI-aware PM decision-quality metrics** (Wilson intervals, insufficient-sample flag); live-session ingest bridge makes off-host/uninstrumented agents measurable; learning-agent verbatim→lean rotation + TTL worker.
- **Verticals are config-not-code:** `workstreams/<name>/config.yaml` drives charter/overlays/budget/policy/checkers/memory via the role seams (`docs/task-lifecycle.md`, `workstreams/README.md`); cross-workstream request contract; ADR-0018 isolation.
- **Doctrine:** evidence-over-claims validators (ADR-0014); every merge gated by an independent review agent.
- **Spokesman grounding & accountability (ADR-0021):** everything told to the human must be grounded — the Spokesman is a **verify-or-refuse gate** (`spokesman/grounding_gate.py`) checking each claim's evidence against source of truth (verified→relay / unverifiable→withhold+request-proof / contradicted→fabrication); **zero-tolerance** trust ledger (`runtime/trust.py`) revokes a fabricator's human-relay permanently + quarantines it + cascades to the verifier chain; fabrication-rate telemetry (Wilson CI). Adversarially reviewed (26+ attacks caught).
- **Capacity governance (ADR-0022):** deterministic graduated budget zones (warn→throttle→**reserve**→hard) + reserve buffer (only wind_down/escalation spend near the cap) + two-level org+allocation ceiling + burn-rate projection (`runtime/budget.py`); optional config-driven **Capacity Steward** role (flags/recommends, never enforces); budget-aware role charters; `capacity_report` telemetry. PM-accountable.
- **Model substrates:** router (rules-as-data) over Anthropic/OpenAI/Google + budget-open-weight; **Cursor** as an `agentic`-tier coding substrate via its agent-harness CLI (guarded: timeout + fallback to Opus; opt-in via `CURSOR_API_KEY`; no raw HTTP inference endpoint) + swappable coding worker.
- **Self-healing (ADR-0023):** graduated recovery ladder — nudge+grace → progress-aware re-kick → early `task.stuck` escalation → PM re-decomposition into smaller DAG subtasks (bounded → 🛑) → abandon backstop; failure-reason capture → statistically-gated failure-pattern detector → propose durable fix (reviewable) → verify-as-experiment on real traffic. Supervisor stays non-LLM.
- **Skill induction (ADR-0024):** recurring+efficient trajectory patterns → **Curator** induces a candidate skill → dual-source review queue (Curator + Researcher, + Retro handoff) → **human-gated 🔴 promote** to live `skills/` → applied-cohort **efficacy** (`skill.applied` + `skill_efficacy_report`, Wilson CI) drives **keep/tune/retire** proposals. Propose-only + review-gated (ADR-0008); statistically gated; hierarchy deferred.
- **Evaluation harness v2 (statistically honest):** corpus-as-data (`evals/corpus/*.yaml`); **every rate carries `n` + Wilson 95% CI + `INSUFFICIENT(n<30)`** (a 1.0 on n=5 reads `[0.566,1.0] INSUFFICIENT`, not "trustworthy"); **swappable LLM-judge** (dry-run→real, zero code change, replay-cassette proven); **record/replay** VCR; **trajectory-level eval**; telemetry `quality_report`; `python -m evals`; [`docs/evaluation.md`](../docs/evaluation.md) corrects the v1 tiny-n overclaim + keeps the now-vs-go-live boundary.

## Boundary — needs stakeholder input (see [`backlog.md`](backlog.md))

- Model provider keys · monthly budget ceiling · **first vertical/product** · WhatsApp provisioning.
- Everything is **dry-run/keyless** until these land.

## Notes

- Developed from a remote session; host is separate → `state/` (git) is the
  cross-machine substrate ([`README.md`](README.md), ADR-0007). Off-host delegation:
  [`offhost/README.md`](offhost/README.md).
- Host bring-up: `./scripts/onboarding.sh` → `./bootstrap` → `python -m runtime.demo`.
- Known non-blocking nits: [`backlog.md`](backlog.md) "Known follow-up nits".
