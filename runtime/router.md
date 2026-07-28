# `runtime/model/` — registry, router, providers & the single call site (M3b)

The model layer (ADR-0005 registry/router + ADR-0012 centralized instrumentation).
It answers one question — *which model runs this request, and what did it cost?* —
and does so through **one** function so cost and events are never bypassed.

**Runs fully keyless.** With no API keys, every path works via the dry-run
provider; real providers activate the moment a key is present. Nothing here holds
or logs a secret.

## Layout

| File | Purpose |
| --- | --- |
| `registry.py` | `ModelSpec`, `Tier`, `Usage`, `RoutingPolicy`, `Registry`; `load_registry`; `cost_usd` (the single cost function) |
| `router.py` | `route()` / `route_decision()`; `RouteDecision`; `OverBudget`; emits `model.routed` |
| `call.py` | `call_model()` — THE instrumented call site; `select_provider()` |
| `providers/base.py` | `Provider` protocol, `Completion`, `Message` |
| `providers/dryrun.py` | `DryRunProvider` — keyless, networkless, deterministic |
| `providers/{anthropic,openai,google}.py` | thin real adapters (key from env, httpx) |
| `providers/__init__.py` | `ADAPTERS` map + `get_adapter()` |
| `../models.example.yaml` | committed catalog + routing policy (rules-as-data) |
| `../models.yaml` | real catalog (git-ignored) |

## Registry & policy format (rules-as-data — ADR-0005)

The catalog **and** the routing policy live in one YAML, resolved like the policy
engine (env → local → committed example):

1. `$AI_STUDIO_MODELS_FILE` (explicit path)
2. `runtime/models.yaml` (real, git-ignored)
3. `runtime/models.example.yaml` (committed default)

```yaml
models:
  - id: claude-opus-4-8
    provider: anthropic          # names the adapter (and thus the env key)
    tier: pm                     # pm | mid | cheap | embedding
    price_in: 5.0                # USD / 1M input tokens
    price_out: 25.0              # USD / 1M output tokens
    cache_read_multiplier: 0.1   # cached-input tokens bill at price_in × this
    context_window: 1000000
    task_fit: [planning, reasoning, coding]
    provenance: docs/model-shortlist.md
    provenance_date: "2026-07-21"

routing:
  task_types:                    # (task_type, quality) -> tier
    plan: {high: pm, standard: pm, low: mid}
    classify: {high: mid, standard: cheap, low: cheap}
  default: {high: pm, standard: mid, low: cheap}   # unmapped task_types
  tiers:                         # tier -> ordered fallback chain of model ids
    pm: [claude-opus-4-8, gemini-3.1-pro]
    mid: [claude-sonnet-5, gemini-3.5-flash, deepseek-v4.5]
    cheap: [claude-haiku-4-5, gemini-3.1-flash-lite]
    embedding: [text-embedding-005]
  downshift: {pm: mid, mid: cheap}   # cheaper tier used when over budget
```

A `ModelSpec` **never** holds an API key. `provider` only names which adapter
services it; the key is a `.env` entry read inside that adapter (ADR-0011).

## Routing (deterministic)

`route(task_type, quality="standard", *, budget_ctx=None, latency=None, sink=…)`:

1. Resolve `(task_type, quality) → tier` from `routing` (task-type override, else
   `default`). Quality bar vocabulary: `high | standard | low`.
2. Walk that tier's fallback chain; return the **first** model id present in the
   catalog (if the chain lists none present, fall back to any catalog model of
   that tier). Same inputs + same policy ⇒ same choice.
3. Emit a `model.routed` event (chosen model, provider, tier, reason) via the sink.

`latency` (the PM's SLA) is accepted for interface completeness and reserved for
future latency-aware tie-breaking; it does not change today's pick.

### Over-budget (ADR-0006/0012)

If a token `BudgetContext` is supplied and the call `would_exceed` the cap, the
router routes **down** to the policy's `downshift` tier (the decision's `reason`
says so, `downshifted=True`). If there is no cheaper tier to fall to, it raises
`OverBudget` — over-budget is a 🛑 concern; it is never a silent overspend.

## The single instrumentation point (ADR-0012)

`call_model(role, task_type, messages, *, quality, budget_ctx, task_id, conn,
sink, …) -> Completion` is the **only** place a model is called. It:

1. **routes** (emits `model.routed`);
2. **selects a provider** — dry-run if `MODELS_DRY_RUN` is set, the provider has
   no wired adapter, or its key is absent; otherwise the real adapter;
3. **completes** the request (timed for `latency_ms`);
4. **computes cost** = `cost_usd(spec, usage)` (tokens × registry price);
5. **emits `model.call`** — `{model, provider, role, task_id, input_tokens,
   output_tokens, cached_tokens, cost_usd, latency_ms}`;
6. **accounts** — if `conn` + `task_id` are given, adds the call's total tokens to
   that task's `spent_tokens` (`runtime.tasks.add_spent_tokens`). Without a
   `conn`, the DB step is skipped, so the wrapper is fully usable with no DB.

**Agents never call a provider adapter directly.** A direct call would bypass
routing, cost accounting, and the event log — the exact ad-hoc call ADR-0012
forbids. Everything an agent needs from an LLM goes through `call_model`.

Events flow through the injected `EventSink` (same pattern as M2's `enforce.py`):
`DbEventSink` in production, `MemoryEventSink` in tests, `NullEventSink` to drop.

## Cost computation

`cost_usd(spec, usage)` is the single source of cost truth:

```
cost = ( (input_tokens - cached_tokens) × price_in
         + cached_tokens × price_in × cache_read_multiplier
         + output_tokens × price_out ) / 1_000_000
```

Cached input bills at the reduced cache-read rate; embeddings have `price_out: 0`.
Prices are per 1M tokens (docs/model-shortlist.md). Cost is always derived from
the registry, never hard-coded at a call site.

## Dry-run

`DryRunProvider` needs no key and no network. It returns deterministic stub text
and **synthetic** token counts derived from input length (~4 chars/token in,
input//4 out), so the whole route→call→cost→event→spend path runs and is testable
keyless. It is the default whenever a real provider can't run.

## Adding a provider

1. Add `providers/<name>.py` with a class exposing `name`, `available()` (reads
   its own env key), and `complete(model_id, messages, **opts) -> Completion`.
   Read the key **inside** the adapter; never log it, put it on the `Completion`,
   or return it. Import `httpx` lazily so the keyless path never needs it.
2. Register it in `providers/__init__.py`'s `ADAPTERS`.
3. Add the model(s) to `models.yaml` (or the example) with a `provider:` matching
   `name`, prices, context window, `task_fit`, and provenance.
4. Reference the model id in the relevant tier's fallback chain under `routing`.

A model whose `provider` has no wired adapter (e.g. the `openweight` budget entry)
is simply served in dry-run until an adapter is added — the intended keyless
default.

## Env

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` — provider keys, read
  only inside the adapters (see `.env.example`).
- `MODELS_DRY_RUN=1` — force dry-run even when keys are present.
- `AI_STUDIO_MODELS_FILE` — explicit registry path override.

## Tests

```bash
pip install -r runtime/requirements.txt pytest
pytest runtime/tests/                 # no network, no DB required
```

`test_model_registry.py` (load + cost + policy), `test_router.py` (selection,
fallback, over-budget downshift + `OverBudget`, `model.routed` event),
`test_providers.py` (dry-run stubs + synthetic usage, `available()` reflects env,
adapters never touch the network), `test_call.py` (`call_model` emits `model.call`
with a registry-computed cost, keyless, no HTTP). DB accounting bits live in
`test_integration_db.py` and skip cleanly with no Postgres. `httpx.post` is
patched to raise in the provider/call tests, proving no real HTTP is attempted.
