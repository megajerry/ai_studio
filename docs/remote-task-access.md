# Remote task access without LAN (the task gateway)

How a session that is **not on the Mac LAN** — a cloud agent container, a laptop
on someone else's network — works the studio task queue. Decision record:
[ADR-0028](decisions/0028-remote-task-access-gateway.md). Code:
[`gateway/`](../gateway/). For Postgres' own (LAN-only) exposure see
[`db-operations.md`](db-operations.md).

> **The remote never gets a database credential.** It gets a scoped bearer token
> for task verbs — list, enqueue, claim, heartbeat, complete — plus read-only
> agent/status/event views under the same `read` scope (no re-mint required for
> those). Each mutating verb runs host-side through `runtime.tasks`, so the
> canonical lifecycle guard stays the only writer. Postgres remains bound to
> loopback. Remotes may act as **any** studio role (including PM): pass
> `--agent-type=pm` on claim; omit `--assignee` to see the full grabbable pool.

## 1. Host: mint a credential

```bash
# Writes the digest into git-ignored .env automatically (no paste step).
# Prints the one-time secret on stdout — give THAT to the remote.
make gateway-provision IDENTITY=offhost-cursor SCOPES=read,enqueue,claim,complete
# or step-by-step:
#   python3 -m gateway.client mint --identity offhost-cursor --scopes read,enqueue,claim,complete
#   make gateway-up
```

`mint` upserts `TASK_GATEWAY_TOKENS` in `.env` (or `$AI_STUDIO_SECRETS`): same
identity is replaced on re-mint; other identities are kept. Only the SHA-256
**digest** is stored — never the secret. Use `--no-write-env` only if you
deliberately want a dry printout.

It prints JSON including:

| Field | Goes to | Notes |
| --- | --- | --- |
| `token` | the **remote**, once | The secret. Never commit it, never put it in host `.env`. |
| `digest` / `spec` | already written to `.env` | Informational echo of what was stored. |
| `env_file` | — | Path that was updated. |

Pin a token to a vertical when it only has business there (keeps ADR-0018
isolation across the remote boundary):

```bash
python3 -m gateway.client mint --identity video-agent \
  --scopes read,claim,complete --workstreams video
```

Because only the digest is stored, a leaked `.env` yields **no usable
credential** — and the host itself cannot recover the token (mint a new one).

## 2. Host: start the gateway

```bash
make gateway-up          # docker compose --profile gateway up -d --build task-gateway
curl -s localhost:8081/health
# {"status":"ok","service":"task-gateway","tokens_configured":1}
```

It is opt-in (compose profile `gateway`), so a plain `docker compose up` is
unchanged, and it publishes on **`127.0.0.1` by default** — not reachable from
anywhere yet, which is the point.

## 3. Host: expose it through the authenticated tunnel

The gateway is designed to sit behind a tunnel; it never needs an open inbound
port on the Mac and it never terminates its own TLS.

```bash
# Named tunnel (recommended: stable hostname, survives restarts)
cloudflared tunnel run --url http://127.0.0.1:8081 <tunnel-name>

# Quick tunnel (ephemeral hostname; fine for a smoke test)
cloudflared tunnel --url http://127.0.0.1:8081
```

Recommended hardening, in order of value:

1. **Cloudflare Access in front of the hostname** (service token or IdP) — a
   second, independent factor before a request ever reaches the gateway.
2. Keep `TASK_GATEWAY_RATE_PER_MIN` at the smallest value the remote can live
   with.
3. Rotate a token by replacing its entry in `TASK_GATEWAY_TOKENS` and restarting
   the service; revoke *immediately* (before rotation completes) by striking the
   identity in the trust ledger — `runtime.tasks.grab_task` then refuses it
   (ADR-0021).

`TASK_GATEWAY_BIND_IP` exists for LAN-only setups. **Never** set it to a public
interface: that is the `0.0.0.0/0` mistake ADR-0017 already rejected, one layer
up.

## 4. Remote: use the queue

The client is stdlib-only — no dependency install, and it can be copied as a
single file (`gateway/client.py`) next to an agent with no repo checkout.

```bash
export TASK_GATEWAY_URL=https://tasks.example.com   # the tunnel hostname
export TASK_GATEWAY_TOKEN=…                         # the minted secret

python3 -m gateway.client whoami
python3 -m gateway.client ready --workstream productivity
python3 -m gateway.client agents                       # who is running what
python3 -m gateway.client studio-status                # queue pulse (test filtered)
python3 -m gateway.client events --limit 20            # recent event types/ids
python3 -m gateway.client agents-env                   # non-secret host markers
python3 -m gateway.client enqueue --workstream productivity --type work.docs \
    --payload '{"goal": "draft the remote-access runbook"}'
# Act as PM (or any role) — do not default-narrow to the offhost pool:
python3 -m gateway.client claim --workstream productivity --agent-type pm
python3 -m gateway.client heartbeat <task-id>       # while working
python3 -m gateway.client complete <task-id> --status merged --result '{"summary": "…"}'
```

A token pinned to exactly one workstream may omit `--workstream` on every verb —
the gateway resolves it from the credential. A token that is unpinned (or pinned
to several) must name it: an unnamed destination answers `422 workstream_required`
rather than being guessed at.

> **Careful with `claim` when verifying.** `claim` grabs *real* queued work — it is
> the whole point of the gateway. Verify against a throwaway workstream
> (`--workstream gw-check-$(date +%s)`) so a smoke test cannot walk off with a live
> `pm.tick`. If one does get claimed, hand it back with a `up_for_grabs`
> transition (`clear_claim=True`) rather than leaving it held.

Or in Python:

```python
from gateway.client import TaskGatewayClient

client = TaskGatewayClient.from_env()
task = client.claim(workstream="productivity")["task"]
if task:
    client.heartbeat(task["id"])
    client.complete(task["id"], result={"summary": "done"})
```

Raw HTTP works just as well (`Authorization: Bearer …`):

```bash
curl -s -X POST "$TASK_GATEWAY_URL/v1/tasks" \
  -H "Authorization: Bearer $TASK_GATEWAY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"workstream":"productivity","type":"work.docs","payload":{"goal":"…"}}'
```

### What a remote can and cannot do

| Can | Cannot |
| --- | --- |
| List ready / waiting / for-review tasks, read one by id | Run SQL, see payloads on other verticals when pinned |
| Pull agent status, studio pulse, recent event types, non-secret env markers (`read`) | Read event bodies, DSNs, tokens, or API keys |
| Enqueue a task (priority/budget/payload all clamped) | Outrank host work, mint an unbounded budget, write a status directly |
| Claim + start any grabbable task as its own identity (any role via `--agent-type`) | Claim while its identity is revoked / quarantined |
| Heartbeat / complete a task **it holds** | Touch a task another worker holds |

A claimed remote task is an ordinary queue row: the supervisor re-kicks it if the
heartbeat goes stale, and every hop lands in `task_transitions` + the event log
attributed to the token identity.

## 5. Verification checklist

Run through this after any change to `gateway/` (the first two are the automated
proof; the rest are the host-only checks the test suite cannot make).

**Automated** (`pytest gateway/tests/ -q`; the DB group skips without Postgres):

- [ ] `gateway/tests/test_auth.py` — spec parsing rejects a pasted plaintext
      secret / unknown scope / bad digest; digest-only authentication; scopes;
      workstream pinning (incl. "pinned means not widened"); rate-limit refill,
      per-identity isolation, limit-before-authorization, and no bucket
      consumption by unauthenticated traffic.
- [ ] `gateway/tests/test_api.py` — every task endpoint is 401 without a token,
      401 on a wrong token, **503 with no tokens configured** (fail closed), 403
      for a missing scope, 403 across workstreams, 429 with `Retry-After`; body /
      payload caps; identifier-shaped input only; a DB outage answers a generic
      503 that contains no DSN; tokens never appear in logs while identities do;
      unauthenticated denials open **no** DB connection.
- [ ] `gateway/tests/test_gateway_db.py` — against a live Postgres: the full
      enqueue → list → claim → heartbeat → complete path, the exact canonical
      transition sequence, `gateway.access` / `gateway.denied` audit rows,
      claim-ownership refusal, pinning against real rows, host-pinned work not
      stolen, dependency gating, priority/budget/limit clamps, and a revoked
      identity fenced out.

**Host-only** (cannot be verified off-host):

- [ ] `curl -s localhost:8081/health` returns `tokens_configured >= 1`.
- [ ] `docker compose --profile gateway ps task-gateway` shows `healthy`.
- [ ] `lsof -nP -iTCP:8081 | grep LISTEN` shows the bind on **127.0.0.1**, not `*`.
- [ ] Postgres is untouched by this feature: `docker compose exec postgres cat
      "$(docker compose exec -T postgres psql -U aistudio -Atc 'show hba_file')"`
      contains no `0.0.0.0/0` and no `trust`; `PG_BIND_IP` is still loopback.
- [ ] Through the tunnel hostname (i.e. genuinely off-LAN): `whoami` succeeds, a
      request with no token is 401, and enqueue → claim → complete round-trips.
- [ ] The task the remote created is visible on the host
      (`psql … "select claimed_by, status from tasks where id = …"`) with
      `claimed_by` = the token identity.
- [ ] Revoking the identity (trust strike) makes a subsequent `claim` return no
      task while other identities still claim.

### Last run on the host — 2026-07-27

The list above stays unchecked on purpose — it is the checklist to re-run, not a
record. What that run produced on the studio Mac (Colima), against the live spine:

- `pytest runtime/tests spokesman/tests gateway/tests evals -q` → **1107 passed**
  (104 of them `gateway/`, DB group included — Postgres was reachable).
- `python -m runtime.readiness` → **5 PASS / 0 FAIL**, compose check listing
  `task-gateway` among the services.
- `docker compose --profile gateway up -d task-gateway` → `healthy`;
  `/health` reported `tokens_configured: 3`; `lsof -nP -iTCP:8081` showed
  `127.0.0.1:8081 (LISTEN)` — never `*`.
- Postgres untouched: `pg_hba.conf` held only `local`, `127.0.0.1/32`, `::1/128`
  and the compose bridge `172.28.0.0/16`, all `scram-sha-256`; no `0.0.0.0/0`, no
  `trust`; published port bound to loopback.
- **Genuinely off-LAN** via a `cloudflared` quick tunnel: no token → `401`, wrong
  token → `401`, `whoami` → the minted identity, and
  enqueue → list → claim → heartbeat → complete round-tripped over HTTPS.
- Host-side confirmation of that remote round trip: the row reached `merged` with
  `claimed_by = <token identity>`, `task_transitions` recorded the canonical
  `up_for_grabs → claimed → in_progress → ready_for_review → approved → merged`,
  and `events` held one `gateway.access` row per verb.
- Denials over the same tunnel: read-only token enqueue → `403 missing_scope`;
  pinned token outside its pin → `403 workstream_denied` (read *and* enqueue);
  40 KB payload → `413`; 25 rapid reads at `rate=60/min burst=5` → `429` with
  `Retry-After`. Each landed in `events` as `gateway.denied` with the identity and
  reason, and **no** payload anywhere contained token material.
- The revocation fence (a struck identity claims nothing while others still do) is
  covered by `test_gateway_db.py` against the same live DB rather than by hand — it
  needs a throwaway identity, since a strike is deliberately hard to undo.

## 6. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| every call `503 no_tokens_configured` | `TASK_GATEWAY_TOKENS` empty in the container | set it in `.env`, recreate the service (fail-closed is working as designed) |
| `401 unknown_token` | wrong/rotated secret, or the spec holds the secret instead of its digest | re-`mint`; the third spec field must be 64 hex chars |
| `403 missing_scope` | token lacks the verb's scope | mint with the scope; don't widen an existing token silently |
| `403 workstream_denied` | the token is pinned and the request named a workstream outside the pin | use a workstream the token is pinned to (never widen a token silently) |
| `422 workstream_required` | unpinned token (or pinned to several) with no `--workstream` | name the workstream; only a single-workstream pin can be inferred |
| `403 not_owner` | the task is held by someone else (or by nobody) | claim it first; a remote may only finish its own work |
| `429` | rate limit | back off (`Retry-After`), or raise `TASK_GATEWAY_RATE_PER_MIN` deliberately |
| `411 content-length required` | a chunked client | send a declared body length (the bundled client does) |
| `503 runtime store unavailable` | Postgres down/unreachable from the container | `make ps` / `make health`; the gateway degrades rather than leaking driver text |
| container starts then exits | malformed `TASK_GATEWAY_TOKENS` | read the startup log — a spec typo fails loudly on purpose |
