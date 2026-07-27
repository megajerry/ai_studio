# 0028 — Non-LAN remote task access: a token-gated task gateway (not remote SQL)

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

[ADR-0010](0010-offhost-remote-agent.md) made the off-host agent **git-only**: it
cannot see the host's services or DB, so it coordinates through `state/` in git.
That was right for a session with *no* path to the host, but it means a remote
session cannot use the real coordination substrate — the task queue — so remote
work is invisible to the supervisor, the lifecycle telemetry, and the event log
until a branch lands.

[ADR-0017](0017-db-resilience-and-remote-access.md) opened Postgres to **LAN
hosts** by CIDR allowlist (`PG_ALLOWED_HOSTS`, scram-sha-256, loopback bind by
default). That covers a second machine on the same network. It does **not** cover
the case the stakeholder actually has: a remote agent session (e.g. a Cursor
cloud container, a laptop on a hotel network) that is **not on the Mac LAN**, has
an ephemeral egress IP, and must still enqueue / list / claim work.

The naive fix — publish Postgres to the internet — is rejected outright (below).
This ADR decides what replaces it.

## Threat model

**Assets.** (1) Task-queue *integrity* — the canonical lifecycle (ADR-0015) is the
studio's correctness guarantee; (2) the event log's completeness as an audit
record; (3) DB credentials; (4) task payloads / event bodies, which can carry
stakeholder-private content (ADR-0011); (5) the host itself (Postgres is a
privileged local process with `COPY`, large-object, and extension surface).

**Adversaries.**

| # | Adversary | Capability |
| --- | --- | --- |
| A1 | Internet scanner | Finds any listening port / hostname within hours; brute-forces credentials; exploits unauthenticated pre-auth surface. |
| A2 | Leaked remote credential | The remote environment is *less trusted than the host*: an env var in an ephemeral cloud container can leak via logs, a hostile dependency, or prompt injection into the agent itself. |
| A3 | Hostile/confused remote agent | Holds a *valid* credential but misbehaves — floods the queue, claims work it doesn't own, writes states the state machine forbids, or reads another vertical's data (ADR-0018). |
| A4 | Network observer | Reads/modifies traffic in transit. |

**Consequence of the credential being a DB credential.** This is the decisive
point. Postgres authenticates a *connection*, then grants that connection the
role's **full SQL authority**. A remote holding it can `UPDATE tasks SET
status='merged'` — an ad-hoc status write, which invariant 4 and ADR-0015 forbid
outright — `DELETE FROM events` (destroying the audit record invariant 6 depends
on), read every payload, and exhaust connections. No network-level mechanism
(VPN, tunnel, mTLS) constrains any of that: they authenticate the *path*, not the
*verb*. So under A2/A3 "remote DB access" means "remote root on the studio's
state", and the host password must live in the remote environment, which
invariant 5 forbids ("secrets never reach an agent").

## Options considered

| Option | Auth | Authority granted | New code | Verdict |
| --- | --- | --- | --- | --- |
| Public Postgres (`0.0.0.0/0`) | scram | Full SQL | none | **Rejected.** A1 finds it; already refused in code by `render-pg-hba.sh`. |
| Tailscale (mesh VPN) → Postgres | Node key + pg scram | Full SQL | none (config) | Rejected as the primary path. Strong *path* auth, but A2/A3 still get full SQL (invariants 4/5 broken); an ephemeral cloud node needs an auth key + `tailscaled` (often unavailable/rootless-hostile), and its tailnet IP is not stable enough for a `/32` allowlist, so `PG_ALLOWED_HOSTS` degrades to a broad CIDR. Kept as an **optional** transport for a *trusted* long-lived machine. |
| cloudflared TCP tunnel → Postgres | CF service token + pg scram | Full SQL | none (config) | Rejected for the same authority reason, plus the client needs `cloudflared access tcp` running locally. |
| **Token-gated HTTPS task gateway** | Scoped bearer token over the existing tunnel | **Only the lifecycle verbs**, through `runtime.tasks` | one small service | **Chosen.** |

## Decision

> **Remote sessions get a least-authority *verb* API over HTTPS — never a
> database connection.** Postgres stays bound to loopback / LAN-allowlist exactly
> as ADR-0017 specifies; nothing about its exposure changes.

`gateway/` is a small FastAPI service (`gateway/app.py`) that exposes the task
queue as five verbs and nothing else:

| Verb | Endpoint | Scope |
| --- | --- | --- |
| list | `GET /v1/tasks/ready`, `/waiting`, `/review`, `GET /v1/tasks/{id}` | `read` |
| enqueue | `POST /v1/tasks` | `enqueue` |
| claim | `POST /v1/tasks/claim` | `claim` |
| heartbeat | `POST /v1/tasks/{id}/heartbeat` | `claim` |
| complete | `POST /v1/tasks/{id}/complete` | `complete` |

Every handler calls the **existing** `runtime.tasks` functions, so the canonical
state machine and its single guard (`runtime.tasks.transition`) remain the only
way task state ever changes: there is **no raw-SQL path through this service**,
and it exposes no endpoint that can write a status directly. The gateway is a
*tool* in the ADR-0004 sense (the remote agent asks; the host performs the side
effect and holds the credential), which is why it satisfies the invariants that
remote SQL breaks.

### The security gates (all enforced in `gateway/auth.py`, all tested)

1. **Bearer token required, fails closed.** No `Authorization: Bearer` → 401.
   **Zero tokens configured → every task endpoint 503**, so a misconfigured
   deploy is unusable, never open.
2. **Tokens are stored as SHA-256 digests** (`TASK_GATEWAY_TOKENS`,
   `identity:scopes:sha256hex`). The host env holds no usable plaintext
   credential; a leaked `.env` does not yield a working token. Comparison is
   `hmac.compare_digest` on the digest (constant-time).
3. **Scopes.** `read` / `enqueue` / `claim` / `complete`, per token. Missing
   scope → 403. A read-only remote cannot mutate the queue.
4. **Workstream restriction.** A token may be pinned to specific workstreams;
   listing/enqueueing/claiming outside them → 403. Preserves vertical isolation
   (ADR-0018) across the remote boundary. A pin of exactly one workstream is also
   the *implied* target when a request omits one — inference only ever narrows to
   what the credential already fixes; an unpinned (or multi-pinned) token with no
   named workstream is refused (422) rather than guessed at.
5. **Identity binding + attribution.** The token's identity *is* the
   `worker_id`/`claimed_by`/`agent_id`, so every remote action is attributable in
   `task_transitions` and the event log; the ADR-0021 trust ledger already fences
   a `revoked`/`quarantined` identity out of `grab_task`, so revocation covers
   remotes for free.
6. **Claim ownership.** `heartbeat`/`complete` only affect a task **this
   identity holds** (403 otherwise) — a remote cannot finish another worker's
   work (`runtime.tasks.complete_task` has no owner check of its own, so the
   gateway enforces it).
7. **Per-identity rate limit** (token bucket, `429` + `Retry-After`) so a
   compromised token cannot flood the queue or brute-force ids.
8. **Bounded input.** Body size cap, payload byte cap, task-`type`/workstream
   character allowlist, priority clamp, `budget_tokens` cap, `limit` clamp.
9. **No credential/driver leakage.** A DB outage answers `503 {"detail":
   "runtime store unavailable"}`; driver text, DSNs and env values never reach a
   response. Tokens are never logged (only the identity is).
10. **Loopback bind by default** (`TASK_GATEWAY_BIND_IP=127.0.0.1`). Reachability
    is provided *only* by an authenticated tunnel (cloudflared named tunnel —
    already a dependency for the Spokesman webhook; Cloudflare Access in front is
    supported and recommended as a second factor). TLS is terminated by the
    tunnel, which answers A4.

### Observability

Two body-free event types (`gateway.access`, `gateway.denied`) record identity,
verb, scope, decision code and HTTP status — never token, payload or DB text. The
mutating verbs additionally emit the normal `task.created` / `task.transition`
events, so remote work is as replayable as host work (invariant 6).

### What this does *not* change

Postgres exposure, `pg_hba` rendering, the LAN allowlist and the degraded-mode
contract are untouched (ADR-0017 stands as written). ADR-0010's rule that the
host **never blocks** on a remote also stands: the gateway is an *additional*
inbound path for a remote that happens to be online, not a dependency — if it is
down, remotes fall back to `state/offhost/` over git exactly as before.

### Amendment (2026-07-27)

Remotes are not confined to an "offhost executor" niche: a claim may name any
`agent_type` (including `pm`). The assignee pool still defaults to `offhost`
(also matching unassigned) so host-pinned work is not stolen. Read-only
observability (`/v1/agents/status`, `/v1/studio/status`, `/v1/events/recent`,
`/v1/agents/env`) is served under the existing `read` scope so already-minted
tokens need no rotation; pinned tokens get workstream-scoped studio status.
Prod vs test queue rows are labeled via `payload.traffic`
([ADR-0030](0030-prod-vs-test-traffic-tag.md)).

## Consequences

- A non-LAN remote session can participate in the real queue (enqueue / list /
  claim / heartbeat / complete) with a revocable, scoped, rate-limited token and
  **no DB credential** — see [`docs/remote-task-access.md`](../remote-task-access.md).
- The remote client is `gateway/client.py`: **stdlib-only** (urllib) plus a CLI,
  so a fresh cloud container needs no dependency install to use it.
- Cost: one more authenticated HTTP surface to keep patched, and a verb API that
  must grow deliberately (every new endpoint is new remote authority — adding one
  is an ADR-level decision, not a convenience).
- The gateway is **opt-in** (`docker compose --profile gateway up`), so the M0
  spine and a plain `docker compose up` are unchanged.
- ADR-0010 gains a second, non-blocking channel (git remains the fallback);
  ADR-0017's remote-SQL path stays LAN-only and is now explicitly *not* the
  answer for non-LAN remotes.
