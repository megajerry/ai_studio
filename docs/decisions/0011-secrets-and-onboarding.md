# 0011 — Secrets, credentials & cold-start onboarding

- **Status:** Accepted
- **Date:** 2026-07-21

## Context

This is a **public repository.** It must never contain personal information
(names, phone numbers, stakeholder identity) or credentials (API keys, access
tokens, passwords). But the running studio genuinely needs those — model-provider
keys, WhatsApp Business credentials, a tunnel token, the stakeholder's WhatsApp
number, generated infra passwords. The remote/off-host agent works on a different
machine and cannot hold host secrets at all.

## Decision

**No personal data or secrets in the repo, ever.** The repo holds only
`.env.example` (placeholders + docs). Real values are collected at runtime by a
**cold-start onboarding flow** and stored **locally, git-ignored**:

- **Onboarding** (`scripts/onboarding.sh`, doc [`../onboarding.md`](../onboarding.md))
  runs on first boot (and whenever config is missing). It prompts for the required
  keys/credentials/personal config and writes them to a git-ignored file:
  - default: `.env` in the repo root (matched by `.gitignore`), `chmod 600`; or
  - an **external** path outside the repo via `AI_STUDIO_SECRETS`
    (e.g. `~/.ai_studio/secrets.env`) — preferred for anything highly sensitive.
- **Secrets never reach an agent** (existing invariant): tools read them from the
  environment / secret store and call external services on the agent's behalf.
- **Personal info is local-only too** — e.g. the stakeholder's WhatsApp number
  lives in `.env`/external secrets, never in a tracked file, status doc, or
  event log committed to git.
- **The off-host agent must not fetch or persist host secrets.** It operates
  git-only; anything secret stays on the host.

## Consequences

- A fresh clone is safe to make public; it carries no credentials.
- Cold-start requires an onboarding step before the studio can talk to any
  external service — this is step 0 of the genesis sequence.
- `.gitignore` must robustly exclude secret files; onboarding writes with tight
  permissions and refuses to write secrets into tracked paths.
- Rotating a key = re-run onboarding (or edit the local file); nothing to scrub
  from git history because it was never committed.
