# Studio status

_Last updated: 2026-07-22 (remote session)_

## Phase

**Design / bootstrap.** Architecture-of-record established; no runtime code yet.

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; onboarding flow + **M0 infra spine implemented** (docker-compose + bootstrap + health check) — **pending verification on the host** (not runnable from the remote session). **M1 event log + task queue implemented** (`runtime/`: Postgres schema + typed data-access + migrator + tests) on branch `runtime/eventlog` — **pending host verification against a live Postgres**. **M2 policy engine + tool layer implemented** (`runtime/`: capabilities/tiers, rules-as-data policy, tool registry, confined FilesystemTool, refusing ShellTool, enforced `invoke` path emitting events) on branch `runtime/policy-tools` — pure/tool/enforce tests green off-host; **pending host verification** (event emission against a live Postgres via `DbEventSink`). **M3a supervisor + scheduler implemented** (`runtime/supervisor.py` + `runtime/scheduler.py`: the non-agent liveness layer — re-kick stale tasks / force-fail on exhausted retries emitting `task.rekicked`/`task.failed_exhausted`; PM-pulse `pm.tick` enqueue-without-pileup; migration `0003_task_retries.sql`; launchd `KeepAlive` templates in `infra/launchd/`) on branch `runtime/supervisor` — unit tests green off-host (72 passed, DB tests skip cleanly); **pending host verification** (migrate + DB re-kick/fail/tick tests + launchd load). **M3b model registry + router + provider abstraction + the single instrumented model-call wrapper implemented** (`runtime/model/`: `registry.py` rules-as-data catalog + routing policy + `cost_usd`; `router.py` deterministic `route()` emitting `model.routed`, budget downshift / `OverBudget`; `providers/` — `DryRunProvider` (keyless, synthetic tokens) + thin anthropic/openai/google adapters reading keys from env; `call.py` `call_model()` the ONLY call site — route→provider→complete→cost→`model.call` event→`spent_tokens`; `models.example.yaml` seeded from the shortlist, real `models.yaml` git-ignored) on branch `runtime/router` — **runs fully keyless (dry-run)**; unit tests green off-host (110 passed, 22 DB tests skip cleanly; `py_compile` clean; no network attempted); **pending host verification** (DB `spent_tokens` accounting via `DbEventSink` against a live Postgres). **M3c minimal roles + worker implemented — the studio now OPERATES END-TO-END in dry-run** (`runtime/roles/`: `pm.py` confidence-gate + enqueue one `work.demo` (emits `pm.planned`), `executor.py` does a policy-gated `filesystem` write + a `call_model` dry-run call, `verifier.py` the INDEPENDENT read-only verify→commit gate; `runtime/worker.py`: `run_once` claim→dispatch→heartbeat→verify→commit with bounded re-enqueue + `run()`/`main()`; `runtime/demo.py` `python -m runtime.demo`; `pm`/`executor`/`verifier` roles added to `policy.example.yaml` at least privilege; `runtime/roles.md`) on branch `runtime/roles-worker` — the loop uses ONLY tools-via-`invoke`, models-via-`call_model`, coordination-via-tasks/events (no agent-to-agent calls, no direct tool/provider calls, no host side effects outside a tool); **verify→commit enforced**; runs fully keyless. Unit tests green off-host (**124 passed, 23 DB tests skip cleanly**; `py_compile` clean; no network); **pending host verification** (worker full-loop DB e2e `test_worker_full_loop_pm_to_done` + `python -m runtime.demo` against a live Postgres). **M4 four-layer memory implemented + VERIFIED on a live Postgres** (`runtime/memory/`: `Scope`/`MemoryLayer`/`MemoryItem`; `embed.py` registry-routed embeddings with a keyless DETERMINISTIC dry-run embedder + structural google/openai/voyage adapters; `vector.py` `PostgresVectorStore` brute-force cosine over scope-filtered rows + a `QdrantVectorStore` structural stub; `api.py` `remember`/`recall` scope-enforced + `add_lesson`/`recall_lessons` for the Retro corpus; migration `0005_memory.sql` `memory_items` with `embedding double precision[]`, no pgvector/Qdrant dep) on branch `runtime/memory` — **runs fully keyless**; scope isolation enforced (episode/project/knowledge/longterm read BY SCOPE, never crossing workstream/project/episode or layer); events carry counts/ids only (never memory text or vectors). **Verified against a live Postgres: `python -m runtime.migrate` applied 0005 (idempotent); `pytest runtime/tests/` = 170 passed, 0 skips with DATABASE_URL set** (11 memory DB tests run + pass; 11 pure-logic tests). **M5 search gateway implemented + VERIFIED on a live Postgres** (`runtime/search/`: `providers.py` `SearchProvider` protocol + `SearchResult` + keyless DETERMINISTIC `DryRunSearchProvider` + structural tavily/exa/brave adapters (key from env, lazy httpx, not tested); `cache.py` pure `query_hash`/`is_expired` + Postgres `SearchCache`; `gateway.py` `search()` = the single policy-gated, always-cached call site — Request→Policy(`net.fetch`)→Cache→provider→Cache-store→Memory, emitting `search.denied`/`search.cache_hit`/`search.cache_miss`/`search.provider_call` (counts/latency/provider only — never bodies/query/keys); `config.py` registry-as-data `search.example.yaml`, git-ignored `search.yaml`; migration `0006_search_cache.sql`) on branch `runtime/search` — provider-agnostic + swappable via config, **runs fully keyless** (dry-run), a cache hit within TTL makes NO provider call, a role without `net.fetch` is denied with no provider call/no cache write. **Verified live: `python -m runtime.migrate` applied 0006 (idempotent); `pytest runtime/tests/` = 185 passed, 0 skips with DATABASE_URL set** (5 search DB tests run + pass incl. cache-miss→hit-no-call + deny-no-write + migration-idempotent; 10 search pure-logic tests). **M7 learning loop (Retro + lesson injection) implemented + VERIFIED on a live Postgres** (`runtime/roles/retro.py` `run_retro` distills ≤3 lessons from an episode trail into Knowledge memory + emits `retro.completed` with COUNT only; `runtime/roles/lessons.py` `inject_lessons` auto-injects recalled lessons into PM/Executor prompts in a bounded/scoped `### Lessons` section, behavior-preserving with none; `worker.py` triggers a Retro on terminal work via `WORKER_RETRO=on_fail|always|off`, **no retro-loop**) on branch `runtime/retro-learning` — **`pytest runtime/tests/` = 228 passed, 0 skips with DATABASE_URL set**; **`python -m runtime.demo` prints both "studio operated end-to-end" AND "studio learned"** (failure → lesson distilled → injected into the next PM prompt). |

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
- **M2 runtime approval mechanism — implemented + VERIFIED on a live Postgres** on
  `runtime/approvals` (see [`runtime/approvals.md`](../runtime/approvals.md)). The
  human-in-the-loop grant loop that lets a 🔴 (NEEDS_APPROVAL) action actually
  proceed after a human approves — the runtime half of ADR-0006's 🛑 "Approve
  (blocks)" (Spokesman/WhatsApp wiring is a **separate later task**). `runtime/approvals.py`:
  `request_approval` (durable `pending` row, **idempotent per `request_fingerprint`**
  = stable hash of `task_id`+tool+sorted-caps), `resolve_approval`
  (approve/deny, guarded to `pending`), `find_grant` / `consume_grant`
  (**one-shot**: a grant authorizes exactly ONE execution), `pending_approvals` /
  `pending_digest` (read side for the future Spokesman), plus `compute_fingerprint`
  (pure). `enforce.invoke` gains an opt-in `conn`: on NEEDS_APPROVAL it checks
  `find_grant` FIRST — grant → execute + `consume_grant` + `tool.invoked`
  (noting `approval_id`); no grant → persist `pending` + PEND. The no-`conn` path
  is unchanged (ephemeral pend, DB-free unit tests). `worker.py` parks a PENDING
  work task as `blocked` (approval_id in `result`) and STOPS; `resume_approved`
  (hooked into the run loop) re-queues a task once its approval is `approved`
  (emit `approval.resumed`) and **fails** it on `denied`; task helpers
  `block_task` / `requeue_blocked_task` / `find_blocked_tasks` in `tasks.py`;
  migration `0007_approvals.sql` (forward-only, idempotent, indexed on `status` +
  `request_fingerprint`). A 🔴 action cannot execute without an explicit
  human-approved, un-consumed grant matching its fingerprint — no auto-approve, no
  bypass of the policy gate; `approval.*` events carry ids/role/tool/tier/reason
  only (never arg values or secrets). **Verified live: `python -m runtime.migrate`
  applied 0007 (idempotent); `pytest runtime/tests/` = 247 passed, 0 skips with
  `DATABASE_URL` set** (13 approval tests: pure fingerprint + full-DB
  block→approve→resume→execute→consume→done, deny→fail-no-execution, one-shot
  re-pend, idempotent request, no-secret events); **`python -m runtime.demo` still
  green** (operate + learn).
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
- **M5 — search gateway: implemented + VERIFIED on a live Postgres** on
  `runtime/search` (see [`runtime/search.md`](../runtime/search.md)). Search from
  architecture §9: agents NEVER search directly — every search goes through the one
  policy-gated, always-cached `runtime.search.gateway.search`
  (Request → Policy → [Tavily | Exa | Brave | dry-run] → Cache → Memory). A search
  is a `net.fetch` capability (🟢); a role without it is denied (`search.denied`,
  no provider call, no cache write). Results are keyed by
  `query_hash(normalized query + provider + k)` in `search_cache`; a hit within TTL
  returns stored results and makes NO provider call. Providers are provider-agnostic
  and swap via `search.yaml` (`default_provider`) without touching callers (ADR-0005,
  like the model router); the keyless deterministic `DryRunSearchProvider` is the
  default, real tavily/exa/brave adapters activate when their key is present. Events
  (`search.cache_hit`/`cache_miss`/`provider_call`/`denied`) carry counts/latency/
  provider only — never result bodies, the raw query, or an API key. **Verified
  live:** `python -m runtime.migrate` applied `0006_search_cache.sql` (idempotent),
  then `pytest runtime/tests/` = **185 passed, 0 skips** (DATABASE_URL set) — the
  cache round-trip / cache-hit-no-provider-call / deny-no-cache-write /
  migration-idempotent search DB tests all ran and passed. Host TODO: none required
  to run keyless; optionally set `TAVILY_API_KEY`/`EXA_API_KEY`/`BRAVE_API_KEY` and
  set `default_provider` to swap off dry-run.
- **Skills layer — implemented + VERIFIED on a live Postgres** on `runtime/skills`
  (see [`runtime/skills.md`](../runtime/skills.md) + [`skills/README.md`](skills/README.md)).
  The Agent Skills open standard (ADR-0008, architecture §14): a `SKILL.md` =
  `---`-fenced YAML frontmatter (`name`/`description` required + `triggers`/
  `when_to_use`/`reviewed`/`source`/`resources`) + a markdown instruction body.
  `runtime/skills/`: `models.py` (`Skill`/`SkillError` + relevance `matches`),
  `loader.py` (`parse_skill`/`load_skill` — malformed frontmatter → clear
  path-qualified `SkillError`, never a crash), `registry.py`
  (`SkillRegistry.discover(root)` walks `SKILL.md` under the skills root — default
  repo `skills/`, `$AI_STUDIO_SKILLS_DIR`-overridable — skipping malformed ones;
  `select(query, limit)` returns ONLY relevant skills, ranked + capped for context
  discipline, ADR-0013), `inject.py` (`compose_prompt`/`compose` inject ONLY
  relevant **reviewed** skills into a bounded `### Skills` section — unreviewed
  ones skipped + logged, `allow_unreviewed=True` to override + warn). **Skills are
  INSTRUCTIONS only** — loading/injecting never executes a resource/script; any
  action still goes through the policy-gated `invoke`. Three reviewed in-repo
  example skills ship: `define-success-criteria` (PM), `retrospective` (Retro),
  `code-review` (Reviewer). Wired minimal + behavior-preserving: `run_pm_tick`
  takes an optional `skills=` registry and composes its confidence-gate prompt
  with the relevant reviewed skill; `worker.run()` + `runtime.demo` discover the
  registry once and thread it to the PM. **Verified live: `pytest runtime/tests/`
  = 206 passed, 0 skips with DATABASE_URL set** (21 new skills pure-logic tests:
  parse valid, malformed→clear-error-no-crash, select only-relevant+limit,
  inject reviewed+relevant/exclude unreviewed+irrelevant, example skills all
  load+reviewed, PM prompt composition); **`python -m runtime.demo` still prints
  "studio operated end-to-end"** (skills=3 discovered + injected).
- **M7 — learning loop (Retro + lesson injection): implemented + VERIFIED on a
  live Postgres** on `runtime/retro-learning` (see
  [`runtime/roles.md`](../runtime/roles.md) "The learning loop"). Closes ADR-0003
  failure mode (c) "not learning from mistakes over time" **structurally**:
  `runtime/roles/retro.py` `run_retro` reads a finished episode's event trail
  (`read_events`), runs a traceability-only dry-run `call_model(role=retro)`, and
  **distills 1-3 concise lessons deterministically** (`distill_lessons`; bounded
  at `MAX_LESSONS=3`, single pass, no reflection loop — a failed episode adds a
  prompt-level prevention lesson, a clean pass records what worked), storing each
  in the **Knowledge** memory layer via `memory.add_lesson`; emits
  `retro.completed` carrying the lesson **COUNT + task ref only — never the lesson
  text**. `runtime/roles/lessons.py` `inject_lessons`/`compose_lessons` is the
  deterministic *apply-the-lesson* step (mirrors `skills.inject.compose_prompt`):
  before PM/Executor act, `recall_lessons(conn, workstream, query, k)` and inject
  the relevant lessons into a bounded, delimited `### Lessons` section —
  workstream-scoped (+ shared global corpus), and **behavior-preserving** (no
  `conn`/no lessons → base prompt unchanged). `worker.py` triggers a Retro after a
  `work.*` task reaches a terminal state, configurable via `WORKER_RETRO`
  (`on_fail` default | `always` | `off`, adaptive-lite); a `retro` task NEVER
  enqueues another (**no retro-loop**) and `pm.tick` never triggers one.
  **Verified live: `pytest runtime/tests/` = 228 passed, 0 skips with
  DATABASE_URL set** (new `test_retro.py` + `test_lessons.py`: distillation +
  bounds, retro trigger policy on_fail/always/off, no-retro-loop, live-DB
  `run_retro` stores ≥1 lesson + `retro.completed` carries no lesson text, bounded
  + scoped injection, no-lessons path unchanged); **`python -m runtime.demo` prints
  both "OK — studio operated end-to-end" AND "OK — studio learned" —** a forced
  work failure → Retro distills a lesson → `lesson learned: 1` → the next PM prompt
  for that workstream includes the recalled `### Lessons` section.
- Next: the Docker **sandbox** (`SandboxRunner`) so 🔴 `shell` can actually run,
  per-task on-demand worker spawning wired to the scheduler/supervisor, and
  curating external skills (PM/review libraries) through the review gate.

## Open decisions

- **Model provider keys + budget** — reports ready for review:
  [`docs/model-shortlist.md`](../docs/model-shortlist.md) +
  [`docs/cost-model.md`](../docs/cost-model.md) /
  [`outbox/0001-model-key-request.md`](outbox/0001-model-key-request.md).
  (Suggested: Anthropic + Google now, OpenAI soon; start ~$200–300/mo.) Does not block M0.
- WhatsApp provisioning (Cloud API vs Twilio) + tunnel (cloudflared / tailscale).

## Known follow-ups (deferred nits from prior milestones)

Small, non-blocking cleanups noted while building later milestones. The
`chore/hardening` pass resolved (b)(c)(d) plus the new relevance-floor item (e);
(a) remains deferred (discoverability-only, touches many files).

- **(a) M2 — event-type constant consolidation.** *(Deferred — still open.)* M2's
  `runtime/enforce.py` defines its event types as module constants
  (`EVENT_POLICY_DECISION`, `EVENT_TOOL_INVOKED`, `EVENT_APPROVAL_REQUESTED`)
  while M1 uses an `EventType` enum (`runtime/models.py`); M3b likewise adds
  `EVENT_MODEL_ROUTED` / `EVENT_MODEL_CALL` constants. The `events.type` column is
  deliberately free-form text so both styles coexist, but the two conventions
  should be consolidated (one enum, or one constants module) so
  producers/consumers have a single catalog of wire strings. Left deferred: it is
  discoverability-only (no correctness impact) and touches many files.
- **(b) M3a supervisor force-fail race.** ✅ *Done (`chore/hardening`).* The
  supervisor's automatic exhausted-fail (`_default_fail_exhausted`) is now guarded
  to `in_progress` (it calls `complete_task` WITHOUT `force`), so a task that
  self-completes in the scan→write window is no longer clobbered to `failed`; the
  sweep logs a skip instead. `complete_task(force=True)` stays available for
  genuine manual use. Covered by `test_sweep_does_not_clobber_self_completed_task`
  (integration) and `test_supervisor_does_not_clobber_task_completed_before_sweep`
  (concurrency), plus the concurrency re-kick→force-fail escalation test.
- **(c) launchd plists missing the DOCTYPE line.** ✅ *Done (`chore/hardening`).*
  Both `infra/launchd/*.plist` now carry the
  `<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">` line; `plutil -lint` passes.
- **(d) memory pure-logic embedding tests force dry-run.** ✅ *Done
  (`chore/hardening`).* The determinism/similarity tests in
  `runtime/tests/test_memory.py` now pass `embed(..., force_dry_run=True)` so they
  never touch the network even if an embedding key is present during pytest.
- **(e) lesson-recall relevance floor.** ✅ *Done (`chore/hardening`).*
  `memory.recall` / `recall_lessons` and `roles.lessons.inject_lessons` accept an
  optional `min_score` cosine floor (default `None` = no floor, behavior-preserving)
  that drops weakly-matching items. Recommended production value ~0.2 for the
  dry-run embedder (`roles.lessons.RECOMMENDED_MIN_SCORE`); tune per real embedding
  model. Covered by `test_recall_min_score_excludes_below_floor`.
