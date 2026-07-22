# Studio status

_Last updated: 2026-07-21 (remote session)_

## Phase

**Design / bootstrap.** Architecture-of-record established; no runtime code yet.

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; onboarding flow + **M0 infra spine implemented** (docker-compose + bootstrap + health check) — **pending verification on the host** (not runnable from the remote session). **M1 event log + task queue implemented** (`runtime/`: Postgres schema + typed data-access + migrator + tests) on branch `runtime/eventlog` — **pending host verification against a live Postgres**. **M2 policy engine + tool layer implemented** (`runtime/`: capabilities/tiers, rules-as-data policy, tool registry, confined FilesystemTool, refusing ShellTool, enforced `invoke` path emitting events) on branch `runtime/policy-tools` — pure/tool/enforce tests green off-host; **pending host verification** (event emission against a live Postgres via `DbEventSink`). **M3a supervisor + scheduler implemented** (`runtime/supervisor.py` + `runtime/scheduler.py`: the non-agent liveness layer — re-kick stale tasks / force-fail on exhausted retries emitting `task.rekicked`/`task.failed_exhausted`; PM-pulse `pm.tick` enqueue-without-pileup; migration `0003_task_retries.sql`; launchd `KeepAlive` templates in `infra/launchd/`) on branch `runtime/supervisor` — unit tests green off-host (72 passed, DB tests skip cleanly); **pending host verification** (migrate + DB re-kick/fail/tick tests + launchd load). **M3b model registry + router + provider abstraction + the single instrumented model-call wrapper implemented** (`runtime/model/`: `registry.py` rules-as-data catalog + routing policy + `cost_usd`; `router.py` deterministic `route()` emitting `model.routed`, budget downshift / `OverBudget`; `providers/` — `DryRunProvider` (keyless, synthetic tokens) + thin anthropic/openai/google adapters reading keys from env; `call.py` `call_model()` the ONLY call site — route→provider→complete→cost→`model.call` event→`spent_tokens`; `models.example.yaml` seeded from the shortlist, real `models.yaml` git-ignored) on branch `runtime/router` — **runs fully keyless (dry-run)**; unit tests green off-host (110 passed, 22 DB tests skip cleanly; `py_compile` clean; no network attempted); **pending host verification** (DB `spent_tokens` accounting via `DbEventSink` against a live Postgres). |

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
- **M3b — model registry + router + providers + instrumented call wrapper:
  implemented** on `runtime/router` (see [`runtime/router.md`](../runtime/router.md)).
  The model layer (ADR-0005 + ADR-0012): a rules-as-data catalog + routing policy
  in `runtime/models.example.yaml` (Opus 4.8 / Sonnet 5 / Haiku 4.5 / Gemini 3.1
  Pro / 3.5 Flash / Flash-Lite / a budget open-weight entry / Google embeddings);
  a deterministic `route()` that maps `(task_type, quality) → tier → model` with
  fallback chains, emits `model.routed`, downshifts (or raises `OverBudget`) when
  over budget; a `Provider` abstraction with a keyless `DryRunProvider` default
  and thin anthropic/openai/google adapters (keys read from env inside the
  adapter, never logged); and `call_model()` — the SINGLE instrumented call site
  that routes, calls, computes cost from registry prices, emits a `model.call`
  event, and adds tokens to the task's `spent_tokens`. Runs fully keyless.
  Host TODO: `pip install -r runtime/requirements.txt` (adds `httpx`), run
  `pytest runtime/tests/`, and exercise `call_model` with `conn=` + a
  `DbEventSink` against a live Postgres so `model.routed`/`model.call` land in the
  M1 log and `spent_tokens` accrues. Optionally set a real provider key + drop
  `MODELS_DRY_RUN` to smoke-test a live provider call.
- Next: M3c (first on-demand role agent end-to-end — a worker that claims a
  `pm.tick` / task, heartbeats, calls models via `call_model`, and completes,
  materialized per task).

## Open decisions

- **Model provider keys + budget** — reports ready for review:
  [`docs/model-shortlist.md`](../docs/model-shortlist.md) +
  [`docs/cost-model.md`](../docs/cost-model.md) /
  [`outbox/0001-model-key-request.md`](outbox/0001-model-key-request.md).
  (Suggested: Anthropic + Google now, OpenAI soon; start ~$200–300/mo.) Does not block M0.
- WhatsApp provisioning (Cloud API vs Twilio) + tunnel (cloudflared / tailscale).

## Known follow-ups (deferred nits from prior milestones)

Small, non-blocking cleanups noted while building later milestones. None affects
correctness today; batch them into a housekeeping pass.

- **(a) M2 — event-type constant consolidation.** M2's `runtime/enforce.py`
  defines its event types as module constants (`EVENT_POLICY_DECISION`,
  `EVENT_TOOL_INVOKED`, `EVENT_APPROVAL_REQUESTED`) while M1 uses an `EventType`
  enum (`runtime/models.py`); M3b likewise adds `EVENT_MODEL_ROUTED` /
  `EVENT_MODEL_CALL` constants. The `events.type` column is deliberately
  free-form text so both styles coexist, but the two conventions should be
  consolidated (one enum, or one constants module) so producers/consumers have a
  single catalog of wire strings.
- **(b) M3a supervisor force-fail race.** The supervisor's force-fail path could
  guard on `in_progress` to avoid clobbering a task that self-completes in the
  scan→write window (`find_stale_tasks` → `complete_task(..., force=True)`).
  `rekick_task` already guards to `in_progress`; the force-fail branch uses
  `force=True` and so could overwrite a task that finished between the scan and
  the write. Low probability (needs the stale-threshold window), but a guarded
  variant (or a re-check) would close it.
- **(c) launchd plists missing the DOCTYPE line.** The templates in
  `infra/launchd/` omit the `<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">` line. Cosmetic —
  `launchd`/`plutil` accept the files without it — but adding it matches the
  canonical plist format.
