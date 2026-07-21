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
- M0 — infra spine (Docker Compose: Postgres, Redis, Qdrant, scheduler,
  supervisor, observability) so a fresh clone on the host can bootstrap.

## Open decisions

- Model provider keys available on the host (Claude / OpenAI / Gemini / local).
- WhatsApp provisioning (Cloud API vs Twilio) + tunnel (cloudflared / tailscale).
