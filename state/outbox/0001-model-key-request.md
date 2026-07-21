# Request → stakeholder: which model API keys to acquire

**Date:** 2026-07-21 · **Class:** 🛑 approve (input needed; not urgent)

Full report: [`docs/model-shortlist.md`](../../docs/model-shortlist.md).
Economics: [`docs/cost-model.md`](../../docs/cost-model.md).

**Ask:** tell me which model providers you have keys for / want to get. Suggested
default: **Anthropic + Google now, OpenAI soon** (covers PM / executor / cheap
tiers + embeddings, with ≥2 providers so the router is meaningful).

**Budget:** ballpark run-cost is **~$150–300/mo (low activity) → ~$700–1.5k
(medium) → ~$2.5–6k (high)** with caching + tiered routing. If you have a target
monthly ceiling, tell me and I'll set the router policy + caps to it; else I
default to a **$200–300/mo** starting envelope.

Keys stay open until you decide; nothing is hard-coded. This does not block infra
work (M0) — only the model-router wiring.
