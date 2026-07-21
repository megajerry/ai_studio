# Cost & ROI model — for stakeholder review

**Purpose:** turn the [model shortlist](model-shortlist.md) into economics —
ballpark token consumption and $/month under a concrete routing strategy, the
dominant cost drivers, and the levers to pull if it's over budget.

> **These are estimates (±2–3×).** They exist to frame the decision, not to be
> precise. Real numbers come from **telemetry** once the studio runs
> ([ADR-0012](decisions/0012-telemetry-metrics.md)) — which is exactly why
> observability is a day-one requirement. All prices are the 2026-07 snapshot
> from [model-shortlist.md](model-shortlist.md) (USD per 1M tokens, in/out).

## 1. The routing strategy (this *is* the cost strategy)

Match models to roles — most spend should land on cheap/mid tiers, with the
premium model reserved for the highest-leverage role:

| Role | Model tier | Default model | in/out $/1M |
| --- | --- | --- | --- |
| PM / planner | premium | Claude Opus 4.8 | 5 / 25 |
| Executor (general/coding) | mid | Sonnet 5 *or* Gemini 3.5 Flash | 3/15 · 1.5/9 |
| Verifier / classifier / router | cheap | Haiku 4.5 *or* Gemini Flash-Lite | 1/5 · 0.125/0.75 |
| Embeddings | — | Google `text-embedding-005` | 0.006 |

## 2. Unit cost — one "task"

A **task** = one delegated unit of work (research a question, prototype a feature,
review a change). A mid-size agentic task, rough token envelope:

| Component | Model | Input tok | Output tok | Cost (no cache) | Cost (w/ caching) |
| --- | --- | --- | --- | --- | --- |
| PM plan + confidence gate | Opus 4.8 | 15k | 3k | $0.15 | $0.10 |
| Executor (agentic ~8–12 steps) | Sonnet 5 | 150k | 20k | $0.75 | $0.47 |
| Verifier | Haiku 4.5 | 20k | 2k | $0.03 | $0.02 |
| Overhead (routing, retro amortized) | — | — | — | +15% | +15% |
| **Total / task** | | | | **~$1.07** | **~$0.68** |

If the executor runs on **Gemini 3.5 Flash** instead of Sonnet 5:
**~$0.67 (no cache) / ~$0.44 (cached)** per task.

> The **executor's input tokens dominate** — agentic loops replay context every
> step. That single fact drives most of the cost and most of the levers.

Typical blended assumption used below: **~$0.55/task** (mixed tiers, caching on).

## 3. Standing cost — the PM pulse (idle heartbeat)

The PM wakes on a cadence even when little is happening. This is a real,
often-overlooked cost:

| Pulse strategy | Cost/day | Cost/month |
| --- | --- | --- |
| Every 15 min on Opus, no cache (naïve) | ~$17 | ~$500 |
| Every 15 min, cheap **triage** model (Haiku), escalate to Opus only on action | ~$3.4 | ~$100 |
| **Event-driven wake** (only when events arrive) + cheap triage | ~$1–3 | ~$30–90 |

**Recommendation: event-driven + cheap-triage pulse.** Idle cost should be tens of
dollars, not hundreds.

## 4. Monthly cost by activity level

Blended ~$0.55/task (caching on, mixed tiers) + an event-driven pulse (~$50–100/mo):

| Activity | Tasks/day | Task cost/mo | + Pulse | **Total/mo (ballpark)** |
| --- | --- | --- | --- | --- |
| **Low** | 10 | ~$165 | ~$50 | **$150–300** |
| **Medium** | 50 | ~$825 | ~$100 | **$700–1,500** |
| **High** | 200 | ~$3,300 | ~$150 | **$2,500–6,000** |

Worst case (no caching, everything on frontier models) is **~3–5× higher**;
aggressive levers (below) can push **~3–5× lower**. Embeddings are negligible
(<$20/mo even at high volume).

## 5. Budget → capacity

Inverting the model (recommended routing + caching, ~$0.55/task, ~$100/mo pulse):

| Monthly budget | ≈ sustainable throughput |
| --- | --- |
| $200 | ~6 tasks/day |
| $500 | ~24 tasks/day |
| $1,000 | ~55 tasks/day |
| $2,000 | ~115 tasks/day |

## 6. Levers, if it's over budget (ranked by impact)

1. **Prompt caching** — up to −90% on repeated input; ~−30–50% total. Free; enable everywhere.
2. **Model routing / downshift** — reserve Opus for the PM; executors on Flash/Sonnet; verify/classify on Haiku/Flash-Lite. −40–70% vs all-frontier.
3. **Event-driven pulse + cheap triage** — −50–90% of idle cost.
4. **Batch API (−50%)** for non-urgent async work (research, retro).
5. **Context / memory scoping** — smaller executor inputs; loops are input-dominated. −20–40%.
6. **Cap agentic iterations + reflection (~2)** — prevents runaway loops (diminishing returns after ~2, per the reflection research).
7. **Hard per-workstream budget caps** (policy engine + router) — enforce a ceiling; the PM then trades quality/speed within it.
8. **Off-host agent offload** — delegate host-resource-free work to the intermittent remote session (this one) at zero marginal API cost where applicable.
9. **Open-weight self-host** (Kimi K3 / GLM-5.2 / Qwen3) for high-volume cheap tasks — swaps per-token cost for fixed GPU cost at scale.

## 7. ROI framing

Cost scales with **throughput** (tasks); value is **experiments shipped / signal
generated per dollar**. The system is designed to be **budget-bounded**:

- Set a monthly ceiling. The **router + policy budget caps keep spend under it**;
  the PM manages the quality ↔ speed ↔ cost tradeoff within the cap.
- Raising the budget or changing scope is a **🛑 stakeholder approval**
  ([ADR-0006](decisions/0006-stakeholder-comms.md)) — the studio can't silently
  spend more.
- **Telemetry closes the loop**: real token/cost/latency per task and per
  workstream ([ADR-0012](decisions/0012-telemetry-metrics.md)) lets the PM and
  Retro role see where money goes and optimize, and lets the Spokesman report
  actual burn vs. budget.

**Recommended start:** a low cap (**$200–300/mo**), caching on, aggressive
routing, event-driven pulse. Watch real telemetry in Grafana, and only raise the
cap for a workstream that has demonstrated ROI.

## Action for you

This is context for the model-key decision, not a new ask. If a target monthly
budget is already in your head, tell me and I'll set the initial router policy +
caps to it. Otherwise I'll default to the $200–300/mo starting envelope above.
