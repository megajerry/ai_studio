# Off-host session handoff → host (2026-07-26)

**From:** the off-host remote agent (this session; no host access, no secrets, shares
state only via git — ADR-0010). **To:** the execution host, which is coming online today.

## How the remote agent communicates progress while the DB is NOT live yet
The DB-backed runtime channels (event log, `decisions`, `approvals`) can't be reached
off-host, so **git `state/` is the only channel** (ADR-0007). Read these on cold-start:
- **`state/status.md`** — current capabilities + verified test/demo state (source of truth for "what works").
- **`state/backlog.md`** — everything done + what's left (the boundary items).
- **`state/outbox/`** — async decisions awaiting you (see below) — non-blocking.
- **`state/offhost/results/`** — this handoff. All code is merged to `main` and pushed to origin; a `git pull` gets everything.

**Once the DB is live today**, the runtime takes over for live coordination, and any
future off-host work can be replayed into telemetry via the trajectory-ingest bridge
(`python -m runtime.trajectory_ingest`). The host **never blocks** on the off-host agent.

## Current state (verify locally)
- `main` (origin) — platform complete, hardened, keyless/dry-run. **961 tests / 0 skips** on a live Postgres; `python -m runtime.demo` green; `python -m runtime.readiness` **READY** (1 HOST-REQUIRED). ADRs 0001–0026 (0026 pending merge).
- This session added: trajectory observability (0020), Spokesman grounding/accountability (0021), capacity governance (0022), Cursor sandboxed substrate, self-healing recovery ladder (0023), skill induction (0024), async decisions (0025), go-live readiness tooling, and a **3-audit security/correctness hardening sweep that found + fixed 6 real bugs** (approval-gate binding, cursor env-leak/host-exec, budget over-spend TOCTOU, decision park-race, event-log seq loss) — all evidence-reviewed. Plus (pending) a PM build-vs-buy/agile-adoption principle (0026).

## Cold-start on the host (today)
1. `./scripts/onboarding.sh` — collect secrets/config into git-ignored `.env` (no secrets in repo).
2. `./bootstrap` (or `make up`) — start the infra spine + health check.
3. `python -m runtime.migrate` — apply migrations 0001–0016.
4. `python -m runtime.readiness` — cold-start self-check (import/migrate/demo/config-coverage/compose). Green = go.
5. `python -m runtime.demo` — end-to-end acts.
Full runbook: `docs/go-live.md`.

## Async decisions awaiting you (non-blocking — answer whenever)
- `state/outbox/0001-model-key-request.md` — which provider keys + monthly budget ceiling.
- `state/outbox/0002-first-vertical-decision.md` — **the high-value lever:** pick a first vertical/product (archetypes to react to inside).

Nothing here blocks the host bring-up. The platform is ready; it's waiting for real work + keys to run.
