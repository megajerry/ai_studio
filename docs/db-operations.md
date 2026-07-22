# DB operations — outage resilience & remote host-restricted access

How the studio survives a database outage and how to expose Postgres to
authorized LAN hosts (never the internet). Decision record:
[ADR-0017](decisions/0017-db-resilience-and-remote-access.md). Config lives in
[`runtime/db.py`](../runtime/db.py), [`runtime/supervisor.py`](../runtime/supervisor.py),
[`infra/postgres/`](../infra/postgres/) and [`docker-compose.yml`](../docker-compose.yml).

## 1. Degraded-mode contract

> **On DB-unreachable: log degraded, retry with bounded backoff, DON'T crash.**

`runtime/db.py` provides:

- **`DBUnavailable`** — the single degraded signal. Catch *this*, not raw
  `psycopg`/socket errors.
- **`connect_with_retry(url=None, *, attempts=3, base_delay_s=0.5, max_delay_s=30,
  connect_timeout=5, sleep=…, on_retry=…)`** — bounded exponential backoff; each
  attempt is time-bounded so it can't hang; returns an open connection or raises
  `DBUnavailable`.
- **`connect`** (single attempt) and **`can_connect`** (never-raising probe) are
  unchanged.

The always-on loops (worker / scheduler / supervisor / spokesman bridge) already
reconnect on a dropped connection and swallow per-pass errors, so a transient DB
blip cannot stop them; the supervisor additionally routes its connect through
`connect_with_retry` and treats `DBUnavailable`/`OperationalError` as an outage.

Quick manual check of the outage path (points at a dead port → clean degrade,
no crash / no hang; the asserted version is in
`runtime/tests/test_db_resilience.py`):

```bash
python3 -c "
from runtime.db import connect_with_retry, DBUnavailable
dead = 'postgresql://aistudio@127.0.0.1:1/aistudio'
try:
    connect_with_retry(dead, attempts=2, base_delay_s=0.01, connect_timeout=1)
except DBUnavailable as e:
    print('degraded cleanly:', type(e).__name__, '- attempts', e.attempts)
"
```

## 2. Reconnect grace window (anti thundering-herd)

During an outage no worker can heartbeat, so on recovery **every** in-progress
task looks stale at once. Re-kicking them immediately would stampede live
workers. The supervisor therefore, after recovering from a *known outage*, defers
its re-kick sweep for `SUPERVISOR_RECONNECT_GRACE_S` (default **60s**) so live
workers re-heartbeat first; only tasks still stale after the window are re-kicked.

- `GraceTracker` — arms the window only on a reconnect that follows a failure (a
  clean first/steady connect arms nothing → startup re-kicks stay prompt).
- `supervised_sweep` — returns `None` (deferred) while in grace, else the normal
  `SweepResult`.

Supervisor env: `SUPERVISOR_INTERVAL_S`, `SUPERVISOR_STALE_S`,
`SUPERVISOR_MAX_RETRIES`, `SUPERVISOR_RECONNECT_GRACE_S`.

Set the grace window comfortably longer than a worker's heartbeat interval so a
live worker is guaranteed at least one heartbeat inside the window.

## 3. Remote host-restricted DB access

The app resolves the DB from `DATABASE_URL` — remote is simply a connection
string to the LAN host (`postgresql://aistudio@<host>:5432/aistudio`). Server
side, `infra/postgres/` locks access down:

| File | Role |
| --- | --- |
| `pg_hba.conf.template` | scram-sha-256 (never `trust`); local + loopback + compose-subnet trusted-by-CIDR; `@PG_ALLOWED_HOSTS@` marker for the external allowlist |
| `render-pg-hba.sh` | expands `PG_ALLOWED_HOSTS` into `host … scram-sha-256` rules; **refuses `0.0.0.0/0` / `::/0`** |
| `docker-entrypoint-wrapper.sh` | renders the allowlist at start, then execs the stock entrypoint with `-c hba_file=… -c listen_addresses=… -c password_encryption=scram-sha-256` |
| `postgresql.conf` | reference `listen_addresses` + optional TLS |

### Bind it to the LAN, not the internet

`docker-compose.yml` publishes the port as
`${PG_BIND_IP:-127.0.0.1}:${POSTGRES_PORT:-5432}:5432`:

- **Local-first default** (`PG_BIND_IP=127.0.0.1`) — Postgres is reachable only on
  the host; no LAN/internet exposure.
- **LAN access** — set `PG_BIND_IP` to the host's LAN IP (e.g. `192.168.1.10`).
  Never bind a public interface.

### Add an authorized host

1. Add the client's CIDR to `PG_ALLOWED_HOSTS` in your git-ignored `.env`
   (space/comma-separated), e.g. `PG_ALLOWED_HOSTS="192.168.1.42/32"`. Use `/32`
   for a single host or a subnet CIDR for a range. **Never `0.0.0.0/0`.**
2. `docker compose up -d postgres` (recreates the container → re-renders pg_hba).
3. Open the firewall for *only* those hosts (defense in depth), e.g. macOS:
   ```bash
   # pf example — allow the Postgres port only from the allowlisted subnet
   pass in proto tcp from 192.168.1.42 to any port 5432
   block in proto tcp to any port 5432
   ```
   (Adjust for your firewall; the principle is: default-deny, allow only the
   allowlisted hosts.)
4. Verify from the client: `psql "postgresql://aistudio@<host>:5432/aistudio"`.

### Optional TLS (recommended for real LAN use)

1. Provide a server cert/key, mounted read-only (key mode `0600`, owned by the
   postgres user), e.g. `/etc/postgresql/tls/server.{crt,key}`.
2. In `postgresql.conf`: `ssl = on` + `ssl_cert_file` / `ssl_key_file` (see the
   commented block), and mount it via `-c config_file=…`.
3. Switch the allowlist rules from `host` to **`hostssl`** so remote hosts are
   *required* to use TLS. Clients then use `sslmode=require` (or `verify-full`).

### What a reviewer should confirm

- No `trust` and no `0.0.0.0/0` / `::/0` anywhere in the rendered pg_hba.
- Auth is scram-sha-256; `PG_BIND_IP` defaults to loopback.
- Remote access is a specific-host allowlist driven by `PG_ALLOWED_HOSTS`.
- No secrets committed (password / allowlist / bind IP come from `.env`).
