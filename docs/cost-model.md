# Cost & ROI model — for stakeholder review

**Purpose:** turn the [model shortlist](model-shortlist.md) into economics —
realistic token consumption and $/month under a routing strategy, the dominant
cost driver, and the levers to pull if it's over budget.

> **These are estimates (±2–3×), and deliberately not optimistic.** Real numbers
> come from **telemetry** once the studio runs ([ADR-0012](decisions/0012-telemetry-metrics.md))
> — until then we're flying blind (see the callout in §3). All prices are the
> 2026-07 snapshot from [model-shortlist.md](model-shortlist.md) (USD per 1M
> tokens, in/out). Caching and context discipline move real cost more than model
> choice does.

## 1. The one thing that dominates cost: context size

Per-task cost is driven **less by which model you pick than by how many tokens
you feed it** — and in an agentic loop, **context grows every step** as tool
outputs accumulate. A task is not a fixed prompt; its *cumulative* input is the
sum of a context that starts small and balloons.

- A single LLM call might be 20k–150k tokens.
- An 8–12 step agentic task whose context grows to 300k+ can bill **500k–1.5M
  cumulative input tokens** — several times the naïve single-call figure.

So the headline levers (§5) are **caching** and **context scoping**, not model
price. The [routing strategy](#2-the-routing-strategy) matters, but it's second.

## 2. The routing strategy

Match models to roles — most spend on cheap/mid tiers, premium reserved for the
highest-leverage role:

| Role | Model tier | Default model | in/out $/1M |
| --- | --- | --- | --- |
| PM / planner | premium | Claude Opus 4.8 | 5 / 25 |
| Executor (general/coding) | mid *or* budget-open-weight | Sonnet 5 / Gemini Flash / DeepSeek·Qwen API | 3/15 · 1.5/9 · ~0.2–0.9 |
| Verifier / classifier / router | cheap *or* local | Haiku 4.5 / Flash-Lite / local 8B | 1/5 · 0.125/0.75 · ~0 |
| Embeddings | — | Google `text-embedding-005` / local | 0.006 · ~0 |

## 3. Unit cost — one "task"

A **task** = one delegated unit of work. Cost depends overwhelmingly on its
**context weight**, so we model three classes rather than one average:

| Class | Cumulative input / output | Executor | ~$/task (no cache) | ~$/task (cached) |
| --- | --- | --- | --- | --- |
| **Light** (classify, summarize, short research, small edit) | ~20k / 3k | local 8B / Flash-Lite | ~$0.01 | ~$0.005 |
| **Mid** (real agentic task, few tools, scoped context) | ~150–300k / 15k | budget-open-weight API / Flash | ~$0.15–0.60 | ~$0.08–0.30 |
| **Heavy** (long agentic coding/research, context grows to 300k+) | ~600k–1M / 40k | Sonnet 5 / Gemini Flash | **~$2–4** | **~$1.3–2** |

Worked heavy example (Sonnet 5, 800k cumulative in / 40k out): no-cache
`0.8M×$3 + 0.04M×$15 = $2.40 + $0.60 = $3.00`; with an ~80%-cached stable prefix
(cache reads at ~10%) the input drops to ~$0.67 → **~$1.3** + PM/verifier.

> **The earlier v1 of this doc assumed a fixed 150k-input "heavy" task (~$0.55).
> That was optimistic** — it ignored context growth. The numbers above supersede
> it: heavy tasks are ~$1.3–3, not ~$0.55.

### Reality check: this very session

I (the PM session that wrote this) am the **anti-pattern**: one monolithic thread
carrying **~350k+ tokens and growing**, re-billed every turn, to produce ~8 merged
deliverables via ~13 delegated subagent runs. I **can't give exact $ because I'm
not instrumented** — which is the whole argument for [ADR-0012](decisions/0012-telemetry-metrics.md).
Directionally: low-tens of dollars for those 8 deliverables ≈ **~$2–4 each** —
matching the *heavy* row, not the old $0.55. Note the subagents each ran in
**~20–90k** contexts — a fraction of my thread. **That contrast is the ROI game.**

## 4. Standing cost — the PM pulse (idle heartbeat)

| Pulse strategy | Cost/day | Cost/month |
| --- | --- | --- |
| Every 15 min on Opus, no cache (naïve) | ~$17 | ~$500 |
| Every 15 min, cheap **triage** model, escalate only on action | ~$3.4 | ~$100 |
| **Event-driven wake** + cheap triage | ~$1–3 | ~$30–90 |

**Use event-driven + cheap triage.** Idle cost should be tens of dollars.

## 5. Levers, ranked by impact

1. **Context scoping** — the biggest lever, because context size dominates
   (§1). Give each task only its own scoped inputs; **don't carry a monolithic
   history**. Atomic tasks + scoped memory + delegating to small-context
   subagents (this repo's whole design) is what keeps per-task tokens — and thus
   cost — small. This session vs. a scoped subagent is a **4–15× context
   difference.**
2. **Prompt caching** — for agentic loops the large stable prefix is highly
   cacheable (Opus cached input $0.50 vs $5). ~−70–90% of input cost. Free;
   enable everywhere.
3. **Model routing / downshift** — reserve Opus for the PM; executors on
   Flash / budget-open-weight; verify/classify on Haiku/Flash-Lite/local.
4. **Cap agentic iterations + reflection (~2)** — fewer steps = less context
   growth = less cost (compounds with §1).
5. **Batch API (−50%)** for non-urgent async work (research, retro).
6. **Hard per-workstream budget caps** (policy engine + router) — enforce a
   ceiling; the PM trades quality/speed within it.
7. **Off-host agent offload** — delegate host-resource-free work to the
   intermittent remote session at zero marginal API cost.
8. **Flat-rate / self-host substrates** — see §7.

## 6. Monthly cost & honest throughput

With lean pulse (~$40/mo) + aggressive routing/caching, at a **$200/mo** cap
(~$160 for work):

| If the work is mostly… | ~$/task | **Tasks/day @ $200** |
| --- | --- | --- |
| Light | ~$0.005–0.01 | **hundreds** |
| Mid | ~$0.08–0.30 | **~20–50** |
| Heavy (frontier-quality agentic) | ~$1.3–2 | **~2–4** |

**Be clear-eyed:** naïve heavy-task throughput is low, and lower than v1 implied.
Autonomous, multi-step, frontier-quality work is genuinely expensive and frontier
prices are *rising*. The studio's value is not a magic low price — it's the
Productivity workstream **driving tokens-per-useful-outcome down over time**
(scoping, caching, routing), which it can only do once instrumented.

Scaling bands (mixed workload, same levers): **$200–300/mo** = a handful of heavy
+ tens of mid + hundreds of light per day; **~$1k/mo** ≈ 3–5× that; **~$3k+/mo**
for sustained heavy throughput.

## 7. Substrate options: metered API, flat-rate, or self-host

The executor tier is where most volume lands, so its substrate drives ROI:

- **Metered API** — simplest, pay-per-use; costs as modeled above.
- **Flat-rate subscription** (Claude Max / Codex-style, ~$100–200/mo *flat*) —
  for a single power user driving lots of coding, this can beat metered by a wide
  margin: you escape per-token billing entirely. Often the **best $/throughput
  for a solo operator's executor/coding tier.**
  - **Cursor (Ultra, $200/mo flat)** — integrated as the studio's coding/agentic
    **executor substrate** + a guarded router adapter (`cursor-cli`). Marginal
    per-token cost is **$0** under the flat rate, so the registry prices it at 0;
    the $200 is a fixed monthly line item. **Honest caveats:** (1) Cursor has **no
    raw HTTP inference endpoint** — inference is only reachable via its
    **agent-harness CLI** (`cursor-agent -p <prompt> --output-format json`), which
    is *heavier and slower than a plain completion* (it runs a full planning/tool
    loop per call). The harness is run **inside the Docker sandbox** (via a
    `SandboxRunner`, never a raw host subprocess) with an env allowlist that
    forwards **only** `CURSOR_API_KEY` into the container — every other host secret
    is withheld (CLAUDE.md invariants 2 & 5); the key rides the sandbox env by
    name, never the argv. `cursor-agent` needs network egress, so the operator must
    allow it for the sandbox (`SANDBOX_NETWORK`; the hardened default is `none`).
    (2) There is a known 2026 bug where `cursor-agent -p` can
    **hang with no output**, so execution enforces a **hard timeout + automatic
    fallback** to a metered model (never blocks the studio); it also fails closed
    to that fallback when no sandbox/Docker is available (never runs on the host).
    (3) At $200/mo flat it
    **~consumes the entire recommended ~$200/mo cap** on its own — run it as *the*
    coding substrate, not alongside a second full-price executor budget. It
    routes for the `coding` tier only (never cheap/classify/embed).
- **Self-host / usage-rented GPU** — see below; marginal cost → ~0 at volume.

### Self-hosting (local & open-weight)
Fits the local-first thesis. Three regimes:

- **(a) Small local 1B–8B — recommended, near-free.** Runs on the host Mac
  (Ollama 0.19+ uses an MLX backend). Point the **cheap tier** (routing,
  classification, extraction) and **local embeddings** (BGE-M3 / Qwen3-Embedding)
  at it → that whole tier drops toward **$0**. Not for reasoning.
- **(b) 32B–~200B on one Mac — viable executor.** A Mac Studio M3 Ultra / 192 GB
  (~$4k) runs Llama-70B at ~25–30 tok/s fully in unified memory; sweet spot
  32B–200B at 10–30 tok/s for single-user use. Weak at high concurrency. ~6-month
  payback vs. a ~$700/mo API, and the box also does all the (a) work.
- **(c) 200B–300B+ (DeepSeek/Qwen-235B/GLM) — don't run your own GPUs solo.**
  Needs 4–8 datacenter GPUs. **Use a budget open-weight *API* instead** (below).

### On "usage-based GPU rental lowers cost" — yes, and here's the precise picture
The cost killer is **idle time**, not the hourly rate; reserved 24/7 GPUs waste
money on bursty solo workloads. Usage-based billing fixes that — but it comes in
a hierarchy, cheapest first:

1. **Per-token open-weight APIs** (Together / DeepInfra / Fireworks, ~$0.14–0.90/M)
   — this *is* usage-based GPU rental, abstracted to tokens, with the provider
   keeping the fleet warm and batched at a scale you can't match. **Cheapest and
   simplest** for DeepSeek/Qwen/GLM.
2. **Serverless GPU** (Modal / RunPod serverless / Replicate) — per-second,
   scale-to-zero, *you* run the server. Wins only for **custom/fine-tuned weights
   or data control**; you eat **cold-start** (loading hundreds of GB for a 300B
   takes minutes each cold burst → painful for agentic loops; keeping it warm =
   paying for idle again).
3. **On-demand hourly pods** — start/stop yourself; good for *scheduled batch*.
4. **Reserved** — cheapest per-hour, only for steady high utilization.

**Net:** usage-based is the right instinct, and for standard big open models its
cheapest form *is a per-token open-weight API* — rental-by-usage done for you.
Raw serverless/on-demand GPUs win only for custom weights, data residency, or
sustained high volume (>~100M tok/day). Break-even ≈
`(monthly GPU cost × 1.3–2.0 ops factor) ÷ blended API $/token`, utilization-adjusted.
Usage-rented GPUs are also a **no-capex alternative to buying the 192 GB Mac** for
regime (b). The router treats `local`/rented as just another provider, and
telemetry ([ADR-0012](decisions/0012-telemetry-metrics.md)) signals when volume
crosses a break-even.

## 8. ROI framing

Cost scales with **throughput × context weight**; value is **experiments shipped
/ signal generated per dollar**. The system is **budget-bounded**: set a monthly
ceiling; the router + policy caps keep spend under it; raising the budget or scope
is a **🛑 stakeholder approval** ([ADR-0006](decisions/0006-stakeholder-comms.md)).
Telemetry closes the loop so the PM/Retro can push tokens-per-outcome down.

**Recommended start:** a low cap (**$200–300/mo**), caching on, aggressive
scoping + routing, event-driven pulse, cheap tier + embeddings local; executor on
a flat-rate coding plan or budget-open-weight API. Watch real telemetry; raise the
cap only for a workstream that has demonstrated ROI.

## Action for you

Two inputs shape the initial router policy (neither blocks other work):
1. **Monthly budget ceiling** — a number, or I default to $200–300/mo.
2. **Executor substrate** — flat-rate coding subscription (best $/throughput if
   driven hard), metered API (simplest), or self-host/rented GPU (best at volume).
   I'll default to a blend: PM on metered frontier + executor on flat-rate/local.
