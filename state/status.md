# Studio status

_Last updated: 2026-07-22 (remote session)_

## Phase

**Design / bootstrap.** Architecture-of-record established; no runtime code yet.

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; onboarding flow + **M0 infra spine implemented** (docker-compose + bootstrap + health check) — **pending verification on the host** (not runnable from the remote session). **M1 event log + task queue implemented** (`runtime/`: Postgres schema + typed data-access + migrator + tests) on branch `runtime/eventlog` — **pending host verification against a live Postgres**. **M2 policy engine + tool layer implemented** (`runtime/`: capabilities/tiers, rules-as-data policy, tool registry, confined FilesystemTool, refusing ShellTool, enforced `invoke` path emitting events) on branch `runtime/policy-tools` — pure/tool/enforce tests green off-host; **pending host verification** (event emission against a live Postgres via `DbEventSink`). **M3a supervisor + scheduler implemented** (`runtime/supervisor.py` + `runtime/scheduler.py`: the non-agent liveness layer — re-kick stale tasks / force-fail on exhausted retries emitting `task.rekicked`/`task.failed_exhausted`; PM-pulse `pm.tick` enqueue-without-pileup; migration `0003_task_retries.sql`; launchd `KeepAlive` templates in `infra/launchd/`) on branch `runtime/supervisor` — unit tests green off-host (72 passed, DB tests skip cleanly); **pending host verification** (migrate + DB re-kick/fail/tick tests + launchd load). **M3b model registry + router + provider abstraction + the single instrumented model-call wrapper implemented** (`runtime/model/`: `registry.py` rules-as-data catalog + routing policy + `cost_usd`; `router.py` deterministic `route()` emitting `model.routed`, budget downshift / `OverBudget`; `providers/` — `DryRunProvider` (keyless, synthetic tokens) + thin anthropic/openai/google adapters reading keys from env; `call.py` `call_model()` the ONLY call site — route→provider→complete→cost→`model.call` event→`spent_tokens`; `models.example.yaml` seeded from the shortlist, real `models.yaml` git-ignored) on branch `runtime/router` — **runs fully keyless (dry-run)**; unit tests green off-host (110 passed, 22 DB tests skip cleanly; `py_compile` clean; no network attempted); **pending host verification** (DB `spent_tokens` accounting via `DbEventSink` against a live Postgres). **M3c minimal roles + worker implemented — the studio now OPERATES END-TO-END in dry-run** (`runtime/roles/`: `pm.py` confidence-gate + enqueue one `work.demo` (emits `pm.planned`), `executor.py` does a policy-gated `filesystem` write + a `call_model` dry-run call, `verifier.py` the INDEPENDENT read-only verify→commit gate; `runtime/worker.py`: `run_once` claim→dispatch→heartbeat→verify→commit with bounded re-enqueue + `run()`/`main()`; `runtime/demo.py` `python -m runtime.demo`; `pm`/`executor`/`verifier` roles added to `policy.example.yaml` at least privilege; `runtime/roles.md`) on branch `runtime/roles-worker` — the loop uses ONLY tools-via-`invoke`, models-via-`call_model`, coordination-via-tasks/events (no agent-to-agent calls, no direct tool/provider calls, no host side effects outside a tool); **verify→commit enforced**; runs fully keyless. Unit tests green off-host (**124 passed, 23 DB tests skip cleanly**; `py_compile` clean; no network); **pending host verification** (worker full-loop DB e2e `test_worker_full_loop_pm_to_done` + `python -m runtime.demo` against a live Postgres). **M4 four-layer memory implemented + VERIFIED on a live Postgres** (`runtime/memory/`: `Scope`/`MemoryLayer`/`MemoryItem`; `embed.py` registry-routed embeddings with a keyless DETERMINISTIC dry-run embedder + structural google/openai/voyage adapters; `vector.py` `PostgresVectorStore` brute-force cosine over scope-filtered rows + a `QdrantVectorStore` structural stub; `api.py` `remember`/`recall` scope-enforced + `add_lesson`/`recall_lessons` for the Retro corpus; migration `0005_memory.sql` `memory_items` with `embedding double precision[]`, no pgvector/Qdrant dep) on branch `runtime/memory` — **runs fully keyless**; scope isolation enforced (episode/project/knowledge/longterm read BY SCOPE, never crossing workstream/project/episode or layer); events carry counts/ids only (never memory text or vectors). **Verified against a live Postgres: `python -m runtime.migrate` applied 0005 (idempotent); `pytest runtime/tests/` = 170 passed, 0 skips with DATABASE_URL set** (11 memory DB tests run + pass; 11 pure-logic tests). |

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
- **M3c — minimal roles + worker (studio operates end-to-end): implemented** on
  `runtime/roles-worker` (see [`runtime/roles.md`](../runtime/roles.md)). The
  agent-driven loop (architecture §4, ADR-0004/0009) now runs as one story:
  scheduler `pm.tick` → **PM** confidence-gate + enqueue one `work.demo`
  (`pm.planned`) → worker materializes → **Executor** does a policy-gated
  `filesystem` write (`policy.decision`/`tool.invoked`) + a `call_model` dry-run
  call (`model.routed`/`model.call`) → **Verifier** independently checks the
  success criterion → `complete_task(done)` (`task.finished`) — a `work.*` task is
  never `done` until the Verifier passes (verify→commit). Coordination is only via
  the queue/events; tools only via `invoke`; models only via `call_model`; no
  agent-to-agent calls; no host side effects outside a tool. Runs fully keyless
  (dry-run). Host TODO: `pytest runtime/tests/` then, against a live Postgres,
  `python -m runtime.demo` (prints the event trail) and the worker full-loop e2e
  `test_worker_full_loop_pm_to_done`; optionally run `python -m runtime.worker` as
  the on-demand driver alongside the scheduler + supervisor.
- **M4 — four-layer memory: implemented + VERIFIED on a live Postgres** on
  `runtime/memory` (see [`runtime/memory.md`](../runtime/memory.md)). The memory
  subsystem (architecture §7, ADR-0005): four layers Episode → Project →
  Knowledge → Long-term, read BY SCOPE (a recall targets exactly one layer and
  cannot cross a workstream/project/episode boundary; a narrower layer never
  bleeds into a broader query). `remember`/`recall` embed → store/search and emit
  `memory.remembered`/`memory.recalled` with **counts/ids only — never the text
  or the embedding**. Embeddings route through the registry's embedding tier with
  a keyless **deterministic dry-run embedder** (similar text → closer vectors) and
  structural google/openai/voyage adapters (keys from env, not tested). The
  default `PostgresVectorStore` does **brute-force cosine in Python** over the
  scope-filtered rows (`embedding double precision[]`; no pgvector); a
  `QdrantVectorStore` structural stub documents the host swap-in. `add_lesson`/
  `recall_lessons` are the Knowledge-layer helpers for the Retro lessons corpus
  (workstream-scoped + a global `'*'` corpus). Runs fully keyless. **Verified
  live:** `python -m runtime.migrate` applied `0005_memory.sql` (idempotent), then
  `pytest runtime/tests/` = **170 passed, 0 skips** (DATABASE_URL set) — the
  memory DB round-trip / scope-isolation / brute-force-nearest / lessons /
  migration-idempotent tests all ran and passed. Host TODO: none required to run;
  optionally wire `QdrantVectorStore` and set a real embedding key to swap off
  dry-run.
- Next: a real **skills** layer for roles (Agent Skills standard, ADR-0008 — today
  each role's prompt is an inline string template), the Docker **sandbox**
  (`SandboxRunner`) so 🔴 `shell` can actually run, and per-task on-demand worker
  spawning wired to the scheduler/supervisor.

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
