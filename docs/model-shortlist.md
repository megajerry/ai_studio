# Model request report — for stakeholder review

**Purpose:** recommend which model-provider **API keys to acquire**, with
trade-offs, so the studio's model router ([ADR-0005](decisions/0005-model-registry-router.md))
has good options mapped to roles. **This is a request for your review** — pick the
providers you want; I'll seed the registry from your choices.

> **For the economics** (ballpark $/month, cost drivers, and levers), see the
> companion [cost & ROI model](cost-model.md).

> **Snapshot: 2026-07-21.** The frontier reprices and reshuffles roughly monthly
> (four frontier releases landed in the six weeks before this snapshot). Treat all
> numbers as approximate and verify on each provider's official pricing page
> before committing budget. Once the Sourcing agent exists, it keeps this current.

Prices are **USD per 1M tokens, input / output.** Caching (−50–90%) and Batch
(−50%) change real cost materially.

## TL;DR — what I recommend you get keys for

| Priority | Provider | Why | Rough monthly floor |
| --- | --- | --- | --- |
| **Must-have** | **Anthropic** | This env is Claude-native; Opus 4.8 is top-tier for the PM + agentic coding, and one provider covers PM / executor / cheap tiers (Opus 4.8 · Sonnet 5 · Haiku 4.5). | pay-as-you-go |
| **Must-have** | **One embeddings provider** | Memory/Qdrant needs embeddings and **Anthropic has none**. Cheapest good option: Google `text-embedding-005`. Simplest: OpenAI `3-small`. | ~$0–5 |
| **Strongly recommended** | **Google Gemini** | Cheapest flagship-tier ($/quality), cheapest value tier, multimodal, and embeddings — gives the **router real choice** (a router needs ≥2 providers to matter). | pay-as-you-go |
| **Nice-to-have** | **OpenAI** | GPT-5.6 family + dedicated Codex coding model + largest ecosystem; good fallback + coding specialist. | pay-as-you-go |
| **Optional / later** | **Voyage AI** | Best-in-class retrieval embeddings (esp. code). Add if RAG quality matters. | ~$0–20 |
| **Optional / later** | **Self-hosted open-weight** | Kimi K3 / GLM-5.2 / DeepSeek V4.5 / Qwen3 — zero marginal cost & privacy at scale; needs GPU + ops. | GPU cost |

**Minimum viable:** Anthropic + Google (covers every role tier *and* embeddings,
with two providers so routing is meaningful). Add OpenAI when you want a coding
specialist and a third fallback.

## By role (how the router would use them)

Best practice is to **match models to roles**: top-tier for the PM/planner,
mid-tier for executors, cheap/fast for routing & classification.

### PM / planner / hardest reasoning (quality first)
The PM is the highest-leverage role — give it the best.

| Model | Provider | $/1M in/out | Context | Notes |
| --- | --- | --- | --- | --- |
| **Claude Opus 4.8** | Anthropic | 5 / 25 | 1M | Top accessible LMArena; #1 coding; AA Intelligence 61.4. **My default PM.** |
| Claude Fable 5 | Anthropic | 10 / 50 | 1M | Frontier flagship; reserve for the very hardest. |
| GPT-5.6-sol | OpenAI | 5 / 30 | large | Strong terminal coding, long-context, reasoning. |
| Gemini 3.1 Pro | Google | 2 / 12 (≤200k)¹ | 1M | Cheapest flagship-tier; great value. |

¹ Gemini 3.1 Pro rises to ~4 / 18 above 200k tokens.

### Executor / everyday work (balance)
| Model | Provider | $/1M in/out | Context |
| --- | --- | --- | --- |
| **Claude Sonnet 5** | Anthropic | 2 / 10 (intro → 3 / 15) | 1M |
| Gemini 3.5 Flash | Google | 1.5 / 9 | large |
| GPT-5.6-terra | OpenAI | 2.5 / 15 | large |

### Router / classifier / extraction / high-volume (cheap & fast)
| Model | Provider | $/1M in/out | Context |
| --- | --- | --- | --- |
| **Claude Haiku 4.5** | Anthropic | 1 / 5 | 200k |
| GPT-5.6-luna | OpenAI | 1 / 6 | — |
| Gemini 3.1 Flash-Lite | Google | 0.125 / 0.75 | — |
| GPT-5.4-nano | OpenAI | 0.20 / 1.25 | — |

### Coding specialist (for the opencode Worker / Code role)
| Model | Provider | $/1M in/out | Notes |
| --- | --- | --- | --- |
| Claude Opus 4.8 / Sonnet 5 | Anthropic | see above | Top coding quality. |
| GPT-5.3-Codex | OpenAI | 1.75 / 14 | Dedicated long-horizon agentic coding. |
| Kimi K3 / GLM-5.2 / DeepSeek V4.5 | self-host | GPU | Open-weight; Kimi K3 #1 Frontend Code Arena. |

### Embeddings (required for memory; Anthropic has none)
Pick **one** and stick with it — **switching requires re-embedding everything.**

| Model | Provider | $/1M | Notes |
| --- | --- | --- | --- |
| **text-embedding-005** | Google | 0.006 | Best price/performance; "default for 80% of workloads." |
| text-embedding-3-small | OpenAI | 0.02 | Safe default, simplest integration, Matryoshka dims. |
| Gemini Embedding | Google | ~0.15 | MTEB leader; multimodal (text/image/audio/video). |
| Voyage 4 large / voyage-code-3 | Voyage | ~0.12 / 0.18 | Best retrieval quality; code-specialized variant. |

Embedding spend is usually negligible vs. generation; optimize chunking over model.

## Notes & caveats

- **Don't hard-wire one vendor.** The whole point of the router is to swap engines
  as the leaderboard flips (~monthly). Two providers minimum.
- **Prices are rising, not falling** at the frontier — budget accordingly; lean on
  caching/batch and route cheap tiers for volume.
- **Verify per-tool + per-model availability** against official pages before relying
  on any figure here.

## Action for you

Reply with the providers you want (or already have keys for). Suggested default:
**Anthropic + Google** now, **OpenAI** soon. I'll add the chosen models to the
registry and wire `.env.example` accordingly. Until you decide, keys stay open and
nothing is hard-coded.

## Sources (2026-07 snapshot)

LMArena / arena.ai; llm-stats.com; Artificial Analysis Intelligence Index;
Anthropic pricing (platform.claude.com); BenchLM.ai LLM pricing; morphllm.com
LLM API; embedding comparisons (pecollective.com, tokenmix.ai, aimultiple.com).
