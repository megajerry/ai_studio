# 0017 — DB-outage resilience & remote host-restricted DB access

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

Part 2 of the task-lifecycle milestone (deferred in ADR-0015, backlog item 1).
Two gaps remained once the lifecycle state machine landed:

1. **DB-outage resilience.** The store (PostgreSQL) is the coordination substrate
   for every always-on component (worker, scheduler, supervisor, spokesman
   bridge). It may be **remote** (a LAN host, see below) and therefore
   unreachable for stretches. A component that crashes or hangs on a dropped DB
   violates local-first liveness (ADR-0004): the supervisor is the guarantee of
   last resort and must *never* itself die.
2. **Remote access.** Once the DB lives on a separate host, other machines on the
   LAN need to reach it — but Postgres open to the network is a classic
   foot-gun (`0.0.0.0/0` + `trust`). Access must be an explicit host allowlist,
   authenticated, and never exposed to the internet.

**Scope note (added 2026-07-27).** Section 3 below covers **LAN** hosts only. A
remote session that is *not* on the LAN (a cloud agent container, a laptop
elsewhere) is deliberately **not** served by opening Postgres wider: a DB
credential grants full SQL authority, which would bypass the lifecycle guard
(invariant 4) and put a host secret in a remote environment (invariant 5). Those
remotes use the token-gated **task gateway** instead —
[ADR-0028](0028-remote-task-access-gateway.md).

## Decision

### 1. Degraded-mode contract (`runtime/db.py`)

> **On DB-unreachable: log degraded, retry with bounded backoff, DON'T crash.**

- `DBUnavailable` — a single explicit *degraded signal*. Degrade-aware callers
  catch **this** instead of a grab-bag of raw `psycopg`/socket errors.
- `connect_with_retry(...)` — bounded exponential backoff (default 3 attempts,
  0.5s→cap 30s), each attempt bounded by `connect_timeout` so a black-holed host
  can never hang. Returns an open connection or raises `DBUnavailable`. Never
  leaks a raw driver error to a degrade-aware caller. `sleep`/`on_retry` are
  injectable for instant, assertion-friendly tests.
- `can_connect` (never-raising boolean probe) is kept for the test-skip path.

The always-on loops already reconnect on a dropped connection; the contract makes
that uniform and gives one signal to catch.

### 2. Reconnect grace window (`runtime/supervisor.py`)

During an outage **no worker can write a heartbeat**, so on recovery *every*
in-progress task's heartbeat is simultaneously stale. Re-kicking them all at once
is a **thundering-herd stampede** against workers that are alive and about to
heartbeat again. Fix:

- `GraceTracker` — pure, clock-injectable connectivity tracker. A reconnect that
  follows a *known outage* arms a grace window of `SUPERVISOR_RECONNECT_GRACE_S`
  (default 60s); a clean first/steady connect arms nothing (startup re-kicks stay
  prompt).
- `supervised_sweep` — defers the re-kick sweep while inside the grace window, so
  live workers re-heartbeat first; only tasks still stale *after* the window are
  re-kicked. The supervisor loop opens its connection via `connect_with_retry`, so
  an unreachable store degrades (log + retry next interval) rather than crashing,
  and a reconnect arms the grace window.

### 3. Remote host-restricted access (`infra/postgres/`, `docker-compose.yml`)

- `pg_hba.conf.template` — **scram-sha-256 for ALL connections (never `trust`)**;
  remote access is an **allowlist** of specific LAN hosts/CIDRs (**never
  `0.0.0.0/0`**). Local unix socket + loopback + the fixed compose subnet
  (`172.28.0.0/16`, the internal app path) are trusted-by-CIDR + password; the
  external allowlist section is env-driven.
- `render-pg-hba.sh` — expands `PG_ALLOWED_HOSTS` (space/comma CIDRs) into
  `hostssl?`-style rules and **refuses an internet-wide CIDR** (`0.0.0.0/0`,
  `::/0`) with a non-zero exit.
- `docker-entrypoint-wrapper.sh` — renders the allowlist from `PG_ALLOWED_HOSTS`
  at container start, then hands off to the stock entrypoint with
  `-c hba_file=… -c listen_addresses=… -c password_encryption=scram-sha-256`.
  Makes the allowlist env-driven (change + restart).
- `docker-compose.yml` — mounts the three files read-only, sets the entrypoint,
  binds the published port to `PG_BIND_IP` (**default `127.0.0.1`** — local-first,
  not the internet), and pins the internal network subnet to `172.28.0.0/16` so
  the pg_hba internal rule is exact.
- `postgresql.conf` — reference config documenting `listen_addresses` and optional
  TLS (`ssl on` + switch allowlist rules to `hostssl`).

Defence in depth: pg_hba allowlist **and** host port binding **and** the host
firewall — not `listen_addresses` alone. The app resolves the DB from
`DATABASE_URL` (remote = a connection string to the LAN host); no app change
needed.

## Consequences

- Components survive a DB outage cleanly (no crash/hang) and recover; the
  supervisor no longer stampedes workers on reconnect.
- Remote DB access is an explicit, authenticated, internet-closed allowlist that a
  reviewer can read at a glance; adding a host is one env value + a firewall rule.
  It stays **LAN-scoped** — non-LAN remotes go through the task gateway
  ([ADR-0028](0028-remote-task-access-gateway.md)), never a wider `pg_hba`.
- Remote/TLS can't be exercised in the off-host sandbox, so it is verified by
  config review + a template lint/render test; the resilience paths are covered by
  keyless unit tests (dead-port outage → clean `DBUnavailable`; grace window gates
  re-kick). See [`docs/db-operations.md`](../db-operations.md).
- No secrets in the repo (ADR-0011): `PG_ALLOWED_HOSTS`, `PG_BIND_IP`, password
  all come from the git-ignored `.env`.
