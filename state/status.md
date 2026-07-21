# Studio status

_Last updated: 2026-07-21 (remote session)_

## Phase

**Design / bootstrap.** Architecture-of-record established; no runtime code yet.

## Workstreams

| Workstream | Status | Notes |
| --- | --- | --- |
| Productivity (this repo) | 🟡 bootstrapping | Architecture + ADRs written; M0 infra spine not yet built. |

## Next up

- **Genesis task for the host agent:** [`inbox/0001-genesis.md`](inbox/0001-genesis.md).
- M0 — infra spine only (Docker Compose: Postgres, Redis, Qdrant, MinIO, OTel,
  Prometheus, Grafana) so a fresh clone on the host can bootstrap. The supervisor
  + scheduler are a later milestone (they need the event/task model first).

## Open decisions

- **Model provider keys** — report ready for review:
  [`docs/model-shortlist.md`](../docs/model-shortlist.md) /
  [`outbox/0001-model-key-request.md`](outbox/0001-model-key-request.md).
  (Suggested: Anthropic + Google now, OpenAI soon.) Does not block M0.
- WhatsApp provisioning (Cloud API vs Twilio) + tunnel (cloudflared / tailscale).
