# Studio status

_Updated: 2026-07-22. Pointers: **what's left** → [`backlog.md`](backlog.md);
**per-milestone detail + evidence** → `git log`; **design** → `docs/decisions/`._

## Phase

- **Platform complete & operating end-to-end (keyless, on a live Postgres).**
  Buildable backlog exhausted — only stakeholder-boundary items remain (below).
- Verified: **581 tests pass, 0 skips** on a real Postgres; `python -m runtime.demo`
  runs 6 green acts (operate · learn · reviewer-guard · research · config-drives-vertical · critic-consensus).

## Capabilities (one line each; details in git log / docs)

- **Runtime:** event log + task queue · canonical task state machine + dependency DAG + lifecycle telemetry (ADR-0015) · policy engine + capability-gated tools · supervisor + scheduler · model router · worker.
- **Roles:** PM (understand→gate→decompose) · Executor · Verifier · Reviewer/Whistle-blower · **Critic** (forward adversarial partner + PM↔Critic consensus, ADR-0019) · Retro · Researcher · Sourcing.
- **Depth:** four-layer Memory · Search gateway · Skills · Learning loop · human-in-loop approvals · budget enforcement · experiment primitive · coding-worker (opencode in sandbox) · DB-outage resilience + remote allowlist · Spokesman↔runtime.
- **Verticals are config-not-code:** `workstreams/<name>/config.yaml` drives charter/overlays/budget/policy/checkers/memory via the role seams (`docs/task-lifecycle.md`, `workstreams/README.md`); cross-workstream request contract; ADR-0018 isolation.
- **Doctrine:** evidence-over-claims validators (ADR-0014); every merge gated by an independent review agent.
- **Evaluation harness v1 (empirical quality):** coverage wired (`make coverage`); seeded-defect **Verifier precision/recall = 1.0/1.0** on a labeled GOOD/BAD corpus (incl. hallucinated-success + `video_audit` defects); **PM structural decomposition** eval; telemetry-driven **`quality_report`** (`runtime/quality.py`); `python -m evals`; honest now-vs-go-live in [`docs/evaluation.md`](../docs/evaluation.md).

## Boundary — needs stakeholder input (see [`backlog.md`](backlog.md))

- Model provider keys · monthly budget ceiling · **first vertical/product** · WhatsApp provisioning.
- Everything is **dry-run/keyless** until these land.

## Notes

- Developed from a remote session; host is separate → `state/` (git) is the
  cross-machine substrate ([`README.md`](README.md), ADR-0007). Off-host delegation:
  [`offhost/README.md`](offhost/README.md).
- Host bring-up: `./scripts/onboarding.sh` → `./bootstrap` → `python -m runtime.demo`.
- Known non-blocking nits: [`backlog.md`](backlog.md) "Known follow-up nits".
