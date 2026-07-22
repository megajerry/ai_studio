# Studio status

_Last updated: 2026-07-22 (remote session)_

**Real per-workstream budget enforcement** (branch `runtime/budget`, awaiting
merge): `runtime/budget.py` + migration `0010_budgets.sql` (idempotent) — per
`(workstream, period)` `cap_usd`/`cap_tokens` read against **real accrued
`model.call` cost**, enforced at the single model-call site; over cap →
`budget.exceeded` + a 🛑 "raise budget" approval + `OverBudget` (never a silent
overspend), under cap → `budget.checkpoint`; policy `BudgetContext` now gates on
token **and** USD real spend. Evidence: `DATABASE_URL=… pytest runtime/tests/
spokesman/tests/` = **410 passed, 0 skips**; `python -m runtime.demo` exits 0 (four
acts green). Docs: [`runtime/budget.md`](../runtime/budget.md).

## Latest (branch `runtime/task-lifecycle`, ADR-0015 — complete, awaiting merge)

**Canonical task-lifecycle state machine.** Task state is now one canonical
9-state machine (`up_for_grabs → claimed → in_progress → ready_for_review →
approved → merged`, with `blocked` / `reviewer_blocked` / `abandoned`) defined
DB-free in `runtime/task_state.py` and enforced by a single guarded
`runtime.tasks.transition` — **no ad-hoc status writes anywhere**. Migration
`0008_task_lifecycle.sql` (forward-only + idempotent) widened the status CHECK and
mapped legacy rows (`queued→up_for_grabs`, `done→merged`, `failed→abandoned`).
Work is picked up with `grab_task` (grab-by-sort, `FOR UPDATE SKIP LOCKED`,
dependency-gated); `claim_task` = grab + start (loop unchanged). The worker + the
**Verifier as automated reviewer** are one unified dev/review loop (submit →
review → approve → merge; fail → reviewer_blocked → in_progress retry / abandoned;
🔴 → blocked → re-queue). **Task dependencies** (`depends_on` DAG) make the fleet
know what's parallel (`ready_tasks`) vs blocked (`waiting_tasks`); the PM sets
edges when decomposing (cycles rejected). **Lifecycle telemetry**: append-only
`task_transitions` (from/to/agent/latency) + `task_lifecycle` / `task_cost` /
`agent_rollup` / `model_rollup`. Both `grab_task` knobs are **injection-safe**:
`filter` is a structured `{column: value}` mapping (allowlist, values bound as
params) and `sort` is an allowlist parse into `(column, direction[, nulls])` tokens
(ORDER BY built only from validated tokens, never raw SQL). Docs:
[ADR-0015](decisions/0015-task-lifecycle-state-machine.md) +
[`docs/task-lifecycle.md`](task-lifecycle.md). Evidence (rebased on the merged
Docker-sandbox `main`): `DATABASE_URL=… pytest runtime/tests/ spokesman/tests/` =
**395 passed, 0 skips**; `python -m runtime.demo` exits 0 (four acts green, showing
the canonical `ready_for_review → approved → merged` trail). Follow-up (separate):
DB-outage resilience + remote host-restricted DB access.

## Phase

**Runtime operating end-to-end (keyless), against a live Postgres.** The merged
substrate runs as one loop — event log + task queue (M1), policy-gated tools (M2),
supervisor/scheduler (M3a), the single instrumented model call + router/providers
(M3b), the PM → Executor → Verifier → **Reviewer/Whistle-blower** → Retro +
**Researcher** roles + worker (M3c/M7), four-layer memory (M4), search gateway
(M5), and the skills layer.
The **PM now genuinely decomposes** a goal into N work items behind a real
confidence gate (execute / clarify / push-back), not a single hard-coded task.
Evidence: `DATABASE_URL=… pytest runtime/tests/` = **287 passed, 0 skips**;
`python -m runtime.demo` exits 0 across **four acts** (PM decomposes into >1 work
items, all verified done; the learning loop distills + injects a lesson; the
Reviewer passes a clean episode and flags + escalates a hallucinated-success one;
the Researcher mines external best-practice through the policy-gated search gateway
into recallable Knowledge lessons).

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; onboarding flow + **M0 infra spine implemented** (docker-compose + bootstrap + health check) — **pending verification on the host** (not runnable from the remote session). **M1 event log + task queue implemented** (`runtime/`: Postgres schema + typed data-access + migrator + tests) on branch `runtime/eventlog` — **pending host verification against a live Postgres**. **M2 policy engine + tool layer implemented** (`runtime/`: capabilities/tiers, rules-as-data policy, tool registry, confined FilesystemTool, refusing ShellTool, enforced `invoke` path emitting events) on branch `runtime/policy-tools` — pure/tool/enforce tests green off-host; **pending host verification** (event emission against a live Postgres via `DbEventSink`). **M3a supervisor + scheduler implemented** (`runtime/supervisor.py` + `runtime/scheduler.py`: the non-agent liveness layer — re-kick stale tasks / force-fail on exhausted retries emitting `task.rekicked`/`task.failed_exhausted`; PM-pulse `pm.tick` enqueue-without-pileup; migration `0003_task_retries.sql`; launchd `KeepAlive` templates in `infra/launchd/`) on branch `runtime/supervisor` — unit tests green off-host (72 passed, DB tests skip cleanly); **pending host verification** (migrate + DB re-kick/fail/tick tests + launchd load). **M3b model registry + router + provider abstraction + the single instrumented model-call wrapper implemented** (`runtime/model/`: `registry.py` rules-as-data catalog + routing policy + `cost_usd`; `router.py` deterministic `route()` emitting `model.routed`, budget downshift / `OverBudget`; `providers/` — `DryRunProvider` (keyless, synthetic tokens) + thin anthropic/openai/google adapters reading keys from env; `call.py` `call_model()` the ONLY call site — route→provider→complete→cost→`model.call` event→`spent_tokens`; `models.example.yaml` seeded from the shortlist, real `models.yaml` git-ignored) on branch `runtime/router` — **runs fully keyless (dry-run)**; unit tests green off-host (110 passed, 22 DB tests skip cleanly; `py_compile` clean; no network attempted); **pending host verification** (DB `spent_tokens` accounting via `DbEventSink` against a live Postgres). **M3c minimal roles + worker implemented — the studio now OPERATES END-TO-END in dry-run** (`runtime/roles/`: `pm.py` confidence-gate + enqueue one `work.demo` (emits `pm.planned`), `executor.py` does a policy-gated `filesystem` write + a `call_model` dry-run call, `verifier.py` the INDEPENDENT read-only verify→commit gate; `runtime/worker.py`: `run_once` claim→dispatch→heartbeat→verify→commit with bounded re-enqueue + `run()`/`main()`; `runtime/demo.py` `python -m runtime.demo`; `pm`/`executor`/`verifier` roles added to `policy.example.yaml` at least privilege; `runtime/roles.md`) on branch `runtime/roles-worker` — the loop uses ONLY tools-via-`invoke`, models-via-`call_model`, coordination-via-tasks/events (no agent-to-agent calls, no direct tool/provider calls, no host side effects outside a tool); **verify→commit enforced**; runs fully keyless. Unit tests green off-host (**124 passed, 23 DB tests skip cleanly**; `py_compile` clean; no network); **pending host verification** (worker full-loop DB e2e `test_worker_full_loop_pm_to_done` + `python -m runtime.demo` against a live Postgres). **M4 four-layer memory implemented + VERIFIED on a live Postgres** (`runtime/memory/`: `Scope`/`MemoryLayer`/`MemoryItem`; `embed.py` registry-routed embeddings with a keyless DETERMINISTIC dry-run embedder + structural google/openai/voyage adapters; `vector.py` `PostgresVectorStore` brute-force cosine over scope-filtered rows + a `QdrantVectorStore` structural stub; `api.py` `remember`/`recall` scope-enforced + `add_lesson`/`recall_lessons` for the Retro corpus; migration `0005_memory.sql` `memory_items` with `embedding double precision[]`, no pgvector/Qdrant dep) on branch `runtime/memory` — **runs fully keyless**; scope isolation enforced (episode/project/knowledge/longterm read BY SCOPE, never crossing workstream/project/episode or layer); events carry counts/ids only (never memory text or vectors). **Verified against a live Postgres: `python -m runtime.migrate` applied 0005 (idempotent); `pytest runtime/tests/` = 170 passed, 0 skips with DATABASE_URL set** (11 memory DB tests run + pass; 11 pure-logic tests). **M5 search gateway implemented + VERIFIED on a live Postgres** (`runtime/search/`: `providers.py` `SearchProvider` protocol + `SearchResult` + keyless DETERMINISTIC `DryRunSearchProvider` + structural tavily/exa/brave adapters (key from env, lazy httpx, not tested); `cache.py` pure `query_hash`/`is_expired` + Postgres `SearchCache`; `gateway.py` `search()` = the single policy-gated, always-cached call site — Request→Policy(`net.fetch`)→Cache→provider→Cache-store→Memory, emitting `search.denied`/`search.cache_hit`/`search.cache_miss`/`search.provider_call` (counts/latency/provider only — never bodies/query/keys); `config.py` registry-as-data `search.example.yaml`, git-ignored `search.yaml`; migration `0006_search_cache.sql`) on branch `runtime/search` — provider-agnostic + swappable via config, **runs fully keyless** (dry-run), a cache hit within TTL makes NO provider call, a role without `net.fetch` is denied with no provider call/no cache write. **Verified live: `python -m runtime.migrate` applied 0006 (idempotent); `pytest runtime/tests/` = 185 passed, 0 skips with DATABASE_URL set** (5 search DB tests run + pass incl. cache-miss→hit-no-call + deny-no-write + migration-idempotent; 10 search pure-logic tests). **M7 learning loop (Retro + lesson injection) implemented + VERIFIED on a live Postgres** (`runtime/roles/retro.py` `run_retro` distills ≤3 lessons from an episode trail into Knowledge memory + emits `retro.completed` with COUNT only; `runtime/roles/lessons.py` `inject_lessons` auto-injects recalled lessons into PM/Executor prompts in a bounded/scoped `### Lessons` section, behavior-preserving with none; `worker.py` triggers a Retro on terminal work via `WORKER_RETRO=on_fail|always|off`, **no retro-loop**) on branch `runtime/retro-learning` — **`pytest runtime/tests/` = 228 passed, 0 skips with DATABASE_URL set**; **`python -m runtime.demo` prints both "studio operated end-to-end" AND "studio learned"** (failure → lesson distilled → injected into the next PM prompt). **Reviewer / Whistle-blower role implemented + VERIFIED on a live Postgres** (`runtime/roles/reviewer.py` `run_review` — the INDEPENDENT risk/disaster guard, distinct from the Verifier: reads a finished episode's ACTUAL event trail + re-reads its ACTUAL artifact via the policy-gated `invoke(role="reviewer", fs.read)` (reviewer granted only `fs.read`), then computes fact-based risk signals via the pure `assess_risks` — hallucinated success (done/verified claim not backed by the artifact), budget blowout (`spent_tokens` vs `budget_tokens`), repeated failures/re-kicks (`retries` + trail), recurring policy denials, gated 🔴 irreversible/costly actions; emits `review.passed`/`review.flagged` (reasons + counts only, NO secret/arg/body/marker), and on HIGH escalates with 🚨 `review.alarm` + a 🛑 `request_approval` row; `rigorous-review` skill injected into the traceability-only model call but the model's opinion does NOT decide — evidence beats claims (ADR-0014). `worker.py` triggers a `review` task after a terminal `work.*` via adaptive `WORKER_REVIEW=on_risk` (default; fires only on failed/re-kicked/over-budget) `|always|off`; a `review` task dispatches to `run_review` and enqueues nothing — **no review-loop, no review↔retro loop**; `reviewer` role already at least-privilege `fs.read` in `policy.example.yaml`) on branch `runtime/reviewer` — **`DATABASE_URL=… pytest runtime/tests/` = 276 passed, 0 skips** (19 new reviewer tests incl. `test_review_evidence_beats_lying_model_flags_hallucination`: a monkeypatched "looks fine" model does not change the fact-based FLAG); **`python -m runtime.demo` exits 0** with a third act "reviewer guarded (clean passed, hallucination flagged + escalated)". **Researcher role implemented + VERIFIED on a live Postgres** (`runtime/roles/researcher.py` `run_research` — the *learn-from-outside* half of ADR-0003: takes a `research` task's `topic`/`question`, gathers via the policy-gated cached **search gateway** `search(conn, role="researcher", …)` ONLY — never agent-direct (`net.fetch`, keyless dry-run; a role lacking `net.fetch` is DENIED with no provider call/no cache write), runs a traceability-only dry-run `call_model(task_type=research)` whose digest is titles/urls only (no fetched body), and distills ≤3 reusable **lessons** into Knowledge memory via `add_lesson` — recallable + auto-injectable into future work; **adaptive-lite**: a fast-moving domain earns an extra "perishable — re-research" lesson. Optional off-by-default candidate `SKILL.md` draft via the policy-gated `invoke(role="researcher", tool_name="filesystem", op="write")` with frontmatter `reviewed: false` + `source` provenance ⇒ excluded by the skills inject gate (review-before-use, ADR-0008; DENIED under the least-privilege default since `researcher` has no `fs.write` — an explicit granted opt-in). Emits `research.completed` carrying the topic **hash** + counts + a `skill_drafted` bool — NEVER the raw topic, result bodies, or lesson text (invariants 5 & 6). `worker.py` dispatches a `research` task to `run_research` and threads NO `enqueue` seam — **no research-loop**; `researcher` already at least-privilege `fs.read, net.fetch` in `policy.example.yaml`) on branch `runtime/researcher` — **`DATABASE_URL=… pytest runtime/tests/` = 287 passed, 0 skips** (11 new researcher tests incl. gateway-gated search, `net.fetch` denial, recallable lessons, `research.completed` leaks no bodies, drafted skill `reviewed: false` excluded by `filter_injectable`, and no research-loop); **`python -m runtime.demo` exits 0** with a fourth act "researcher mined external best-practice into recallable lessons". |

## Next up

- **Genesis task for the host agent:** [`inbox/0001-genesis.md`](inbox/0001-genesis.md)
  — run `./scripts/onboarding.sh` then `./bootstrap` to verify M0 on the host.
- **Spokesman / WhatsApp channel — v1 + runtime wiring built & VERIFIED on a live
  Postgres.** v1: containerized FastAPI (signature-verified webhook,
  alarm/approve/inform routing + digest, inbox/status state integration; dry-run,
  no creds). **Runtime wiring** (`spokesman/runtime_bridge.py`, architecture §9):
  the Spokesman now aggregates all-workstream state from the live event log —
  `poll_notifications(conn, since_cursor)` reads new events past a monotonic `seq`
  **cursor** and classifies them 🛑 `approval.requested` (batched digest) / 🚨
  `review.alarm` (immediate) / 📣 `task.failed_exhausted`+non-HIGH `review.flagged`
  (feed); `studio_status(conn)` returns live task/approval/spend counts;
  `resolve(conn, id, approve|deny, resolver)` wraps `approvals.resolve_approval`
  (worker `resume_approved` then re-queues/fails the blocked task). Wired into the
  app: `POST /poll` (token-gated) sends 🚨 now + batches 🛑/📣 and persists the
  cursor (git-ignored `state/spokesman/notify-cursor.txt` → no re-notify); inbound
  `approve <id>` / `deny <id>` resolve real approvals and `status` replies with DB
  counts — all keyless dry-run, no secret/arg leakage (notifications built from
  leak-free payload fields only). Read-side `read_events` gained an additive
  `since_seq` cursor param. Evidence: `DATABASE_URL=… pytest runtime/tests/
  spokesman/tests/` = **334 passed, 0 skips** (47 spokesman incl. 17 new
  runtime-bridge tests: pending 🛑 in poll+digest, 🚨 immediate, inbound
  approve→resolved+resume_approved re-queues, deny→denied, status real counts,
  cursor no-dupe, no-secret-leak, `/poll` gated, signed-webhook approve e2e);
  `python -m runtime.demo` exits 0 (four acts green). See
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
- **Validation rigor — evidence over claims: implemented + VERIFIED on a live
  Postgres** on `chore/review-rigor` (see
  [`docs/decisions/0014-validation-rigor.md`](../docs/decisions/0014-validation-rigor.md)
  + [`runtime/roles.md`](../runtime/roles.md) "Validator doctrine"). Encodes an
  evidence-over-claims doctrine into the platform's validator agents: LLMs default
  to accepting stated claims as true, which for a validator (the Verifier today,
  the future Reviewer/Whistle-blower) is a defect. New reviewed in-repo skill
  `skills/rigorous-review/SKILL.md` (`reviewed: true`, triggers
  review/verify/validate/audit/check) captures the doctrine: treat every claim as
  UNVERIFIED until you observe evidence yourself; evidence hierarchy (run+read
  output > read the code path > inspect logs/metrics/DB rows/artifacts, NEVER the
  author's summary/comments/commit message); per-claim verdict
  CONFIRMED/UNVERIFIED/REFUTED (unobtainable ⇒ UNVERIFIED, never approve on trust);
  concrete rules ("tests pass"⇒run+count, "no secrets"⇒grep, "bug fixed"⇒repro no
  longer fails); default to skepticism. The **Verifier** (`runtime/roles/verifier.py`)
  now judges on EVIDENCE — its verdict is the deterministic re-read of the ACTUAL
  artifact against the success criterion, never the Executor's `result.ok` claim —
  and injects `rigorous-review` into its prompt when a skill registry is supplied
  (behavior-preserving with none, mirroring how the PM injects its skill; threaded
  through `worker._handle_work`/`run_once` + `runtime.demo`). ADR-0014 added;
  CONTRIBUTING.md Review step now requires an evidence-based review. **Verified
  live (after rebasing onto main's approvals mechanism): `pytest runtime/tests/`
  = 251 passed, 0 skips with DATABASE_URL set** (4
  new tests: evidence beats a false "done" claim, the doctrine skill loads +
  reviewed + selectable, the Verifier prompt carries the injected doctrine with a
  registry / base-only without one); **`python -m runtime.demo` still prints both
  "OK — studio operated end-to-end" AND "OK — studio learned" (skills=4).**
- **PM decomposition + real confidence gate — implemented + VERIFIED on a live
  Postgres** on `runtime/pm-decomposition` (see [`runtime/roles.md`](../runtime/roles.md)
  "The PM contract"). Makes the ADR-0003 PM real (previously the thinnest stub: one
  discarded dry-run call + a single hard-coded `work.demo`). `runtime/roles/pm.py`
  now obtains a **structured `Plan`** (pydantic: `restated_goal`, `success_criteria`,
  self-scored `confidence` ∈ [0,1], `feasible` + `reason`, `work_items[]` where each
  `WorkItem` = title/type/instructions/`success_criterion`/marker) by PARSING the
  `call_model(role=pm, task_type=plan)` output — defensively (unparseable → safe
  low-confidence fallback, never a crash). The **confidence gate** then branches:
  `not feasible` → `pm.pushback` + a real 🛑 `approvals.request_approval` (no work);
  `confidence < PM_CONFIDENCE_THRESHOLD` (env, default 0.6) → `pm.needs_clarification`
  (no work); else **decompose** → enqueue ONE `work.*` task per item (each with its
  own concrete, marker-based criterion in payload so the Verifier still checks a real
  artifact) + emit `pm.planned` with the item COUNT + task ids (no secret text). The
  keyless dry-run provider gained `build_dry_run_plan` (`runtime/model/providers/dryrun.py`):
  a planning call (`plan_goal` opt) returns a deterministic, parseable 2–3-item plan
  derived from the goal; a real model returns the same schema — no PM code change.
  `call_model`/`enqueue`/`request_approval` are injectable seams so every gate branch
  is unit-tested. **Verified live: `DATABASE_URL=… pytest runtime/tests/` = 257 passed,
  0 skips** (new PM tests: multi-item decomposition with per-item criteria + unique
  markers, injected-plan decomposition, low-confidence→clarify/no-work, infeasible→
  pushback+approval/no-work, unparseable→safe-low-confidence, `pm.planned` carries
  counts/ids not secret text; live-DB `test_pm_pushback_creates_approval_*` +
  `test_worker_full_loop_pm_to_done` now asserting N>1 work items drained to done);
  **`python -m runtime.demo` prints "PM decomposed into 2 work items"** and stays
  green (operate + learn).
- **Docker sandbox RUNNER — implemented** on `runtime/sandbox` (see
  [`runtime/sandbox.md`](../runtime/sandbox.md)). `runtime/sandbox/DockerSandboxRunner`
  implements the `SandboxRunner` protocol behind the `ShellTool` seam: runs a
  command in a throwaway container with `--network none`, non-root user,
  `--read-only` rootfs, `--cap-drop ALL` + `no-new-privileges`, hard
  memory/cpu/pids limits, a kill-on-exceed timeout (exit 124), a scoped single
  bind mount (host root `/` refused), and **only** an explicit env allowlist
  forwarded (no host secrets; ADR-0011). `ShellTool.with_docker_sandbox(...)` wires
  it lazily; the 🔴 tier gate + refuse-without-sandbox guard are unchanged. Config
  via `SANDBOX_*`. Docker imported lazily (not required at import). Unit-tested with
  the docker CLI **mocked** (no real container in tests); real run host-verified via
  `infra/sandbox/Dockerfile` (`docker build -t ai-studio-sandbox:latest infra/sandbox`).
  Host TODO: build the image + smoke-test a real `docker run`. **Still pending:** the
  coding-worker **dispatch** that routes work through this runner.
- Next: the coding-worker dispatch wired to the sandbox runner, per-task on-demand
  worker spawning wired to the scheduler/supervisor, and curating external skills
  (PM/review libraries) through the review gate.

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
