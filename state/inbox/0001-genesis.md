# Genesis instruction — for the first local agent on the host

**To:** the first agent (or human) that clones this repo on the target Mac.
**From:** the remote bootstrap session, 2026-07-21.

You are booting the **Productivity workstream** — the horizontal platform. Before
doing anything, read, in order:

1. [`README.md`](../../README.md)
2. [`CLAUDE.md`](../../CLAUDE.md) — the invariants you must not violate
3. [`docs/architecture.md`](../../docs/architecture.md)
4. [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — the branch→review→merge lifecycle
5. [`docs/bootstrap-sequence.md`](../../docs/bootstrap-sequence.md) — how you cold-start
6. `docs/decisions/` — the ADRs (why things are the way they are)
7. [`docs/onboarding.md`](../../docs/onboarding.md) — cold-start secrets/config

## Step 0: onboarding (secrets & personal config)

Before anything that touches an external service, run `./scripts/onboarding.sh`
to collect API keys / WhatsApp credentials / your WhatsApp number / infra
passwords into a git-ignored local file. **This is a public repo — never commit
credentials or personal info** (ADR-0011). You can build M0 infra before you have
model keys; onboarding is required before the model router or WhatsApp channel.

## Your first task: build M0 (the infra spine)

None of the bootstrap sequence is built yet, and there is no runtime to spawn
agents *from* until you build it. **M0 is Layer 0 only — the infra spine** (the
supervisor/scheduler come in a later milestone). So the genesis task is to
**implement M0**, following the lifecycle in `CONTRIBUTING.md`:

- Branch `infra/compose` (agent-identity `infra`, task `compose`).
- Deliver: `docker-compose.yml` (Postgres + heartbeats, Redis, Qdrant, MinIO,
  OTel, Prometheus, Grafana), `.env.example`, `PREREQS.md`, a `bootstrap` script,
  and `scripts/healthcheck` that self-verifies every container.
- Acceptance bar: a fresh clone can `./bootstrap` and the health check passes on
  this machine (self-sufficient — the remote session cannot verify for you).
- Open a PR, spawn a review agent, iterate to approval, merge, delete the branch.

Then proceed to M1 (event log / task queue), M2 (policy + tools), M3 (the thin
supervisor + first on-demand role agent end-to-end), per the sequence.

## Report back

Write progress to [`status.md`](../status.md) and anything needing the
stakeholder into [`../outbox/`](../outbox/). Ask via the Spokesman channel for
anything 🛑 (scope/budget) — see [ADR-0006](../../docs/decisions/0006-stakeholder-comms.md).
