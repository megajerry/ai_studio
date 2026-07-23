# Studio status

_Updated: 2026-07-22. Pointers: **what's left** → [`backlog.md`](backlog.md);
**per-milestone detail + evidence** → `git log`; **design** → `docs/decisions/`._

## Phase

- **Platform complete & operating end-to-end (keyless, on a live Postgres).**
  Buildable backlog exhausted — only stakeholder-boundary items remain (below).
- Verified: **562 tests pass, 0 skips** on a real Postgres; `python -m runtime.demo`
  runs 5 green acts (operate · learn · reviewer-guard · research · config-drives-vertical).

## Capabilities (one line each; details in git log / docs)

- **Runtime:** event log + task queue · canonical task state machine + dependency DAG + lifecycle telemetry (ADR-0015) · policy engine + capability-gated tools · supervisor + scheduler · model router · worker.
- **Roles:** PM (understand→gate→decompose) · Executor · Verifier · Reviewer/Whistle-blower · Retro · Researcher · Sourcing.
- **Depth:** four-layer Memory · Search gateway · Skills · Learning loop · human-in-loop approvals · budget enforcement · experiment primitive · coding-worker (opencode in sandbox) · DB-outage resilience + remote allowlist · Spokesman↔runtime.
- **Verticals are config-not-code:** `workstreams/<name>/config.yaml` drives charter/overlays/budget/policy/checkers/memory via the role seams (`docs/task-lifecycle.md`, `workstreams/README.md`); cross-workstream request contract; ADR-0018 isolation.
- **Doctrine:** evidence-over-claims validators (ADR-0014); every merge gated by an independent review agent.

## Boundary — needs stakeholder input (see [`backlog.md`](backlog.md))

- Model provider keys · monthly budget ceiling · **first vertical/product** · WhatsApp provisioning.
- Everything is **dry-run/keyless** until these land.

## Notes

- Developed from a remote session; host is separate → `state/` (git) is the
  cross-machine substrate ([`README.md`](README.md), ADR-0007). Off-host delegation:
  [`offhost/README.md`](offhost/README.md).
- Host bring-up: `./scripts/onboarding.sh` → `./bootstrap` → `python -m runtime.demo`.
- Known non-blocking nits: [`backlog.md`](backlog.md) "Known follow-up nits".
