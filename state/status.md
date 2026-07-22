# Studio status

_Last updated: 2026-07-21 (remote session)_

## Phase

**Design / bootstrap.** Architecture-of-record established; no runtime code yet.

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; onboarding flow + **M0 infra spine implemented** (docker-compose + bootstrap + health check) — **pending verification on the host** (not runnable from the remote session). **M1 event log + task queue implemented** (`runtime/`: Postgres schema + typed data-access + migrator + tests) on branch `runtime/eventlog` — **pending host verification against a live Postgres**. **M2 policy engine + tool layer implemented** (`runtime/`: capabilities/tiers, rules-as-data policy, tool registry, confined FilesystemTool, refusing ShellTool, enforced `invoke` path emitting events) on branch `runtime/policy-tools` — pure/tool/enforce tests green off-host; **pending host verification** (event emission against a live Postgres via `DbEventSink`). **M3a supervisor + scheduler implemented** (`runtime/supervisor.py` + `runtime/scheduler.py`: the non-agent liveness layer — re-kick stale tasks / force-fail on exhausted retries emitting `task.rekicked`/`task.failed_exhausted`; PM-pulse `pm.tick` enqueue-without-pileup; migration `0003_task_retries.sql`; launchd `KeepAlive` templates in `infra/launchd/`) on branch `runtime/supervisor` — unit tests green off-host (72 passed, DB tests skip cleanly); **pending host verification** (migrate + DB re-kick/fail/tick tests + launchd load). |

## Next up

- **Genesis task for the host agent:** [`inbox/0001-genesis.md`](inbox/0001-genesis.md)
  — run `./scripts/onboarding.sh` then `./bootstrap` to verify M0 on the host.
- **Spokesman / WhatsApp channel** — **v1 built** (containerized FastAPI:
  signature-verified webhook, alarm/approve/inform routing + digest, inbox/status
  state integration; runs in dry-run with no creds). See
  [`docs/spokesman-whatsapp.md`](../docs/spokesman-whatsapp.md). Remaining: WhatsApp
  Business provisioning (stakeholder credentials + a public tunnel) + host verify.
- **M1 — event log / task queue: implemented** on `runtime/eventlog` (see
  [`runtime/README.md`](../runtime/README.md)). Host TODO: `make migrate` against
  a live Postgres, then `pytest runtime/tests/` (DB tests skip off-host).
- **M2 — policy engine + tool layer: implemented** on `runtime/policy-tools` (see
  [`runtime/policy-tools.md`](../runtime/policy-tools.md)). Agents run tools only
  via `runtime.enforce.invoke`, which gates each call through the policy engine
  (least privilege + 🟢/🟡/🔴 tiers + budget) and emits `policy.decision` /
  `tool.invoked` / `approval.requested`; 🔴 never auto-executes. Host TODO: run
  `pytest runtime/tests/` and exercise `DbEventSink` against a live Postgres so
  the emitted events land in the M1 log.
- **M3a — supervisor + scheduler: implemented** on `runtime/supervisor` (see
  [`runtime/supervisor.md`](../runtime/supervisor.md)). The non-agent liveness
  layer (ADR-0004): `sweep` re-kicks in-progress tasks with a stale heartbeat
  (reset → `queued`, bump `retries`, emit `task.rekicked`) and force-fails ones
  that exhaust `SUPERVISOR_MAX_RETRIES` (emit `task.failed_exhausted`);
  `tick_once` enqueues the `pm.tick` pulse without pileup. No LLM calls. Host
  TODO: `python -m runtime.migrate` (applies `0003_task_retries.sql`), then
  `pytest runtime/tests/` against a live Postgres (re-kick / exhausted-fail /
  tick DB tests), then install the launchd templates from `infra/launchd/`
  (`KeepAlive`) so the OS keeps the supervisor alive.
- Next: M3b (first on-demand role agent end-to-end — a worker that claims a
  `pm.tick` / task, heartbeats, and completes, materialized per task).

## Open decisions

- **Model provider keys + budget** — reports ready for review:
  [`docs/model-shortlist.md`](../docs/model-shortlist.md) +
  [`docs/cost-model.md`](../docs/cost-model.md) /
  [`outbox/0001-model-key-request.md`](outbox/0001-model-key-request.md).
  (Suggested: Anthropic + Google now, OpenAI soon; start ~$200–300/mo.) Does not block M0.
- WhatsApp provisioning (Cloud API vs Twilio) + tunnel (cloudflared / tailscale).
