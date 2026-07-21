# 0012 — Telemetry & metrics are a Productivity product requirement

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

Productivity's job is to optimize other agents' effectiveness — and it can't
optimize what it doesn't measure. Observability is already day-one infra (OTel +
Prometheus + Grafana in M0); this ADR specifies **what must be captured** and
**why**, so the data needed for cost control and optimization exists from the
start rather than being retrofitted.

## Decision

Every agent action, LLM call, and task emits **structured telemetry** via
OpenTelemetry (metrics + traces + logs), persisted (metrics → Prometheus, traces
+ logs → store) and surfaced in Grafana. Required signals:

- **Token usage** — input / output / **cached** tokens per LLM call, tagged by
  `model`, `provider`, `role`, `task_id`, `workstream`. (Foundation of cost.)
- **Cost** — $ per call / task / workstream / day, derived from tokens ×
  registry price ([ADR-0005](0005-model-registry-router.md)).
- **Sessions** — count and duration per role / workstream.
- **Session trajectory** — the ordered steps / tool-calls / events of a task as a
  **trace (spans)**, so we can replay a run and see where tokens/time/stalls/
  retries go.
- **Latency** — per LLM call and per task end-to-end.
- **Reliability** — error rate, retry count, nudge count, escalations, verifier
  pass/fail rate.
- **Routing decisions** — which model was chosen and why (as events).
- **Budget** — consumption vs. cap, per workstream.

**Instrumentation is centralized, not ad-hoc.** All model calls go through the
router/tool layer, which is the single place that records tokens + cost; no agent
makes a direct, uninstrumented provider call. Trace context propagates across the
whole task lifecycle.

## Consumers

- **Cost model** ([cost-model.md](../cost-model.md)) — estimates → actuals.
- **Retro role** — spots inefficiency/failure patterns to optimize.
- **Router / Sourcing** — tunes model selection from observed quality/cost/latency.
- **Spokesman** — reports actual burn vs. budget to the stakeholder.

## Consequences

- The model-call wrapper must emit token/cost metrics + spans on every call.
- A metrics/label schema and a retention policy are needed (define with the
  runtime).
- Grafana dashboards for token/cost/latency/reliability are part of "done" for the
  runtime milestones, not an afterthought.
- This is the concrete reason OTel + Prometheus + Grafana are in M0.
