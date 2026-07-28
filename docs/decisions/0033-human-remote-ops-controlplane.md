# 0033 — Human-operated remote ops control-plane (temporary scaffolding)

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

The studio is **not self-sufficient yet.** When the stakeholder is remote there is
currently **no way to start anything on the host** — the runtime worker /
scheduler / supervisor / trajectory-worker have to be brought up by hand on the
Mac. Two pre-existing gaps compound this:

1. **The runtime services were never on compose.** Only the M0 spine + the
   opt-in `spokesman` and `gateway` services were. The runtime processes ran from
   launchd plists that **never received the DB credential or model-provider keys**
   (those are only substituted into compose services from `.env`). So even a
   local start-up left the worker keyless.
2. **No remote start button.** [ADR-0028](0028-remote-task-access-gateway.md) gave
   a remote session a token-gated way to *enqueue/claim* tasks, but nothing to
   *start the processes that drain the queue*. A remote stakeholder could file
   work into a queue that nobody was running.

The stakeholder made two explicit decisions for a **temporary** unblock:

- **Execution model** — mount the **host Docker socket** into the Spokesman
  container and run the runtime services as **docker-compose services**, so the
  Spokesman (already the stakeholder's remote channel) can drive `docker compose`.
- **Scope** — a **named allowlist** of ops **plus a gated arbitrary `docker`
  escape hatch**; destructive ops require an explicit confirm; everything audited.

This is **explicitly temporary scaffolding with an ACCEPTED security posture** —
it exists only until the studio can start and heal itself, at which point this
control-plane should be retired or replaced with a narrower supervisor.

## Decision

### Part 1 — Runtime services on compose

Add `worker`, `scheduler`, `supervisor`, `trajectory-worker` to
`docker-compose.yml` behind a new **`runtime` profile** (so a plain
`docker compose up` — the M0 spine — is unaffected). All four share one image
(`runtime/Dockerfile`, non-root, `PYTHONPATH=/app`) and differ only by
`command: python -m runtime.<svc>`. Each carries the **same DB + model-key env
block the `spokesman` service already uses** (a shared `x-runtime-env` anchor):
`POSTGRES_*` (`POSTGRES_HOST: postgres`), `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` /
`OPENAI_API_KEY` / `MODELS_DRY_RUN`, `AI_STUDIO_STATE_DIR`, with
`restart: unless-stopped` and `depends_on: postgres healthy`. This closes gap (1):
compose substitutes `${VAR}` from `.env`, so the runtime finally gets its
credentials. **The launchd worker approach is superseded by this** — compose is
the supported, remote-controllable path. (Existing scheduler/supervisor/trajectory
launchd plists are left in place but noted as legacy in the docs.)

### Part 2 — Spokesman gets the docker socket + CLI

The `spokesman` service mounts `/var/run/docker.sock` and the repo (read-only, at
`/app/repo`, so `docker compose` resolves the project + `.env` — via
`AI_STUDIO_COMPOSE_DIR`). `spokesman/Dockerfile` installs the docker CLI + compose
plugin (client only; no daemon runs in the container).

### Part 3 — Ops control-plane (`spokesman/ops.py`)

A small tool that maps commands to a concrete docker/compose **argv** (never a
shell string) and executes it against the mounted socket:

- `ops worker start|stop|status|scale <N>`, `ops ps`, `ops logs <svc>`,
  `ops restart <svc>`, `ops up <svc>`, and `ops docker <args...>` (escape hatch).
- **Token-gated** — reachable via the token-gated `POST /ops` endpoint
  (`X-Spokesman-Token`, fail-closed) and the token-gated web-chat fast-path.
- **Human fast-path ONLY** — the leading `ops` verb is parsed in
  `handle_inbound_command` **before** the model, and only on an authorized channel
  (`ops_authorized`). The **public webhook passes `ops_authorized=False`**, so
  host control stays off the public tunnel.
- **Destructive-guarded** — volume-delete (`down -v`), `prune`, force-`rm`, and
  stopping `postgres` (or a bare `down`) are **blocked** unless an explicit
  `confirm` is supplied; a single message can never destroy volumes.
- **Audited** — every attempt (even a blocked one) emits an `ops.invoked` event
  with the **redacted** argv, exit code, and human identity.
- **Bounded** — hard subprocess timeout + truncated output.

## Invariant reconciliation (the crux)

- **#2 (agents don't touch the host).** The host is touched by a
  **capability-gated tool invoked by an authenticated human**, never by an agent
  autonomously. The conversational LLM (`spokesman.converse`) has **no ops
  capability** and cannot invoke ops: the `ops` verb is intercepted on a
  deterministic string fast-path *before* any model call, and the LLM cannot emit
  a message back into that fast-path. This is asserted by tests (the LLM path is
  monkeypatched to fail if reached for an `ops` command; a conversational message
  that *mentions* ops never touches the runner).
- **#5 (secrets never reach an agent / no secrets in logs).** Provider/DB creds
  stay in the environment. The `ops.invoked` payload carries only operational
  metadata + a **redacted** argv (env-flag values and secret-shaped `KEY=VALUE`
  tokens are stripped); command **stdout/stderr are returned to the human but
  NEVER written to the event log**.
- **#6 (everything emits events).** Every ops attempt is observable via
  `ops.invoked`.
- **Approval tiers.** Ops are 🔴-class host control; they are gated by the human
  token (the human *is* the approval) and destructive ops need a second explicit
  confirm.

## Accepted security posture (temporary)

Mounting the docker socket gives the Spokesman container **host-daemon control**.
This is powerful and is accepted **only** with all of:

1. **Token gate** — `/ops` + the web-chat fast-path require `X-Spokesman-Token` /
   the dashboard token and **fail closed** (401 when unset).
2. **Tunnel restriction** — the ops surface (`/ops`, `/chat`) MUST be kept **off
   the public tunnel** (the cloudflared public ingress exposes only `/webhook` +
   `/health`); ops are reachable only over an authenticated private path.
3. **Human fast-path only** — never the conversational model.
4. **Audit + destructive-confirm** — `ops.invoked` on every attempt; destructive
   ops require an explicit `confirm`.

The residual risk (a leaked token, or Spokesman-container compromise, ⇒ host
control) is **knowingly accepted as temporary scaffolding** until the studio is
self-sufficient. When that lands, retire this control-plane or replace it with a
narrower, non-socket supervisor.

## Consequences

- A remote stakeholder can start/scale/inspect the runtime over an authenticated
  path without SSH to the Mac.
- The runtime finally runs with its DB + model keys (gap (1) fixed).
- One new powerful capability exists; it is fenced by token + tunnel + human
  fast-path + audit + destructive-confirm, and is explicitly temporary.

## Alternatives considered

- **SSH / a shell endpoint** — rejected: unbounded, hard to audit, no
  destructive guard.
- **A bespoke non-socket supervisor API** — the right long-term shape, but more
  than the temporary unblock needs; deferred.
- **Leave the runtime on launchd** — rejected: that is exactly the gap (keyless,
  not remotely startable).
