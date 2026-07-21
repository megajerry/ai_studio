# Onboarding — cold-start secrets & config

This repo is **public** and holds **no** credentials or personal info. Real
values are collected at first boot and stored **locally, git-ignored**
([ADR-0011](decisions/0011-secrets-and-onboarding.md)).

## Run it

```bash
./scripts/onboarding.sh
```

- Prompts for API keys, WhatsApp credentials, tunnel token, your WhatsApp number,
  and local infra passwords (auto-generated if blank).
- Writes them to `.env` (git-ignored, `chmod 600`) — or to an external file if you
  set `AI_STUDIO_SECRETS=~/.ai_studio/secrets.env` first (preferred for sensitive
  values).
- Safe to re-run: existing values are preserved unless you overwrite them. Use it
  to add providers later or rotate a key.
- Refuses to write into any git-tracked path, and never echoes secret values.

Verify nothing is tracked: `git check-ignore .env` should print `.env`.

## What to provide (all optional; fill what you have)

| Group | Vars | How to get |
| --- | --- | --- |
| Models | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `VOYAGE_API_KEY` | provider consoles; see [`model-shortlist.md`](model-shortlist.md) |
| Embeddings | `EMBEDDINGS_PROVIDER` | `google` (cheapest) / `openai` / `voyage` |
| WhatsApp | `WHATSAPP_*`, `STAKEHOLDER_WHATSAPP_NUMBER` | Meta WhatsApp Business Cloud API (see the Spokesman setup runbook) |
| Tunnel | `TUNNEL_PROVIDER`, `CLOUDFLARED_TUNNEL_TOKEN` | cloudflared / tailscale / ngrok — for inbound WhatsApp webhook |
| Infra | `POSTGRES_PASSWORD`, `MINIO_ROOT_*` | auto-generated locally |

## Rules

- **Never** put a real key, token, password, or personal number into a tracked
  file (docs, `state/`, code, commit messages).
- The **off-host agent does not receive host secrets** — it works git-only.
- Tools read secrets from the environment and call services on an agent's behalf;
  **agents never see raw secrets** (see `CLAUDE.md` invariants).
