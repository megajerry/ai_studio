# `runtime/supervisor` + `runtime/scheduler` — the non-agent liveness layer (M3a)

Two small, **non-LLM** always-on singletons (ADR-0004, ADR-0009 "Layer 1"). They
hold no model calls and no business logic — they exist to provide the reliability
an agent cannot provide for itself.

| Module | Job | Runnable as |
| --- | --- | --- |
| `supervisor.py` | *"No task is ever silently dropped."* Re-kick in-progress tasks with a stale heartbeat; force-fail ones that exhaust their retries. | `python -m runtime.supervisor` |
| `scheduler.py` | Ensure the **PM pulse**: enqueue a `pm.tick` task on a cadence (never piling up). | `python -m runtime.scheduler` |

Both loops are thin: they only wrap a single-pass, DB-taking, unit-tested
function (`sweep` / `tick_once`) with env-config + reconnect-on-error + sleep.

## Supervisor — what re-kick guarantees

Each sweep calls `find_stale_tasks(conn, threshold_s)` (M1) — `in_progress` tasks
whose `heartbeat_at` is older than the threshold, **null heartbeats count as
stale**, oldest-first. For each stale task:

- **retries < max_retries → re-kick.** `rekick_task` (in `runtime/tasks.py`)
  resets the row to `up_for_grabs`, clears `claimed_by` and `heartbeat_at`, increments
  `retries`, and emits **`task.rekicked`** — all in one transaction. The runtime
  can then materialize a fresh worker to claim it (ADR-0009). The re-kick is
  guarded to `in_progress`, so a task that changed state between the scan and the
  write is left untouched (no double-kick).
- **retries ≥ max_retries → force-fail.** `complete_task(status='failed',
  force=True)` finalizes it (emits `task.finished`) and we additionally emit
  **`task.failed_exhausted`** with `{retries, max_retries}` so the give-up is
  auditable. This bounds the loop: a genuinely-stuck task cannot churn forever.

Every action emits an event, so the liveness layer is itself fully traceable and
replayable from the event log (invariant 6). A failure handling one task is
logged and does **not** abort the rest of the sweep.

`sweep(conn, threshold_s, max_retries)` is the single testable pass; `run(...)`
just loops it. `run` reconnects on a dropped DB connection and swallows per-sweep
errors — the supervisor is the guarantee of last resort and must not itself die.

### Config (env)

| Env var | Default | Meaning |
| --- | --- | --- |
| `SUPERVISOR_INTERVAL_S` | `30` | Seconds between sweeps. |
| `SUPERVISOR_STALE_S` | `120` | Heartbeat age past which a task is stale. |
| `SUPERVISOR_MAX_RETRIES` | `5` | Re-kicks before force-failing. |

Pick `SUPERVISOR_STALE_S` comfortably larger than a worker's heartbeat cadence so
a live-but-busy worker is never re-kicked out from under itself.

## Scheduler — the PM pulse

`tick_once(conn, workstream="productivity")` enqueues a `pm.tick` task **only if**
no `pm.tick` for that workstream is already active (not merged/abandoned) (avoids
pileup when the PM is slow/wedged — a stuck tick is the supervisor's problem, not
the scheduler's). It returns the new `Task`, or `None` when it skipped. The
enqueue emits `task.created`, so the pulse is traceable. `run(interval_s)` loops
it; env `PULSE_INTERVAL_S` (default `300`s).

The PM is **not** a daemon — "spawning the PM" = enqueuing a task, which the
runtime materializes a PM worker to service (ADR-0009).

## Schema change

Migration `runtime/migrations/0003_task_retries.sql` adds a
`retries int NOT NULL DEFAULT 0` column to `tasks`. Forward-only and idempotent
(`ADD COLUMN IF NOT EXISTS`; the M1 runner also skips already-applied files).
Apply with `python -m runtime.migrate` (see `runtime/README.md`).

## launchd setup (host)

The OS keeps these singletons alive — the supervisor is the one thing that must
never silently die, so it runs under launchd with `KeepAlive`. Templates live in
[`infra/launchd/`](../infra/launchd/):

- `com.aistudio.supervisor.plist`
- `com.aistudio.scheduler.plist`

Each is a **template**: replace `__REPO_DIR__` (absolute path to this checkout)
and `__PYTHON__` (interpreter, e.g. the venv's `bin/python`), then:

```bash
mkdir -p state/logs                       # plists log here
sed -e "s#__REPO_DIR__#$PWD#g" -e "s#__PYTHON__#$(command -v python3)#g" \
    infra/launchd/com.aistudio.supervisor.plist \
    > ~/Library/LaunchAgents/com.aistudio.supervisor.plist
launchctl load ~/Library/LaunchAgents/com.aistudio.supervisor.plist   # start (RunAtLoad)
# ... repeat for com.aistudio.scheduler.plist ...
launchctl list | grep aistudio                                        # verify
launchctl unload ~/Library/LaunchAgents/com.aistudio.<name>.plist     # stop
```

`RunAtLoad` + `KeepAlive` = start now and restart on any exit; `ThrottleInterval`
backs off a crash loop. `DATABASE_URL` (or the `POSTGRES_*` vars) must be
resolvable by the process — export it in the environment or add it to the plist's
`EnvironmentVariables`. Logs land in `state/logs/{supervisor,scheduler}.{out,err}.log`.

## Tests

```bash
pytest runtime/tests/test_supervisor.py runtime/tests/test_scheduler.py   # unit, no DB
pytest runtime/tests/                                                     # + DB tests (skip w/o Postgres)
```

`test_supervisor.py` / `test_scheduler.py` inject fake find-stale / re-kick /
fail / enqueue callables, so the routing (re-kick vs exhausted-fail;
enqueue-vs-skip) is covered with no database. The DB-backed re-kick, exhausted-
fail (event emission), and enqueue-vs-skip paths live in `test_integration_db.py`
and skip cleanly (never hang) when no Postgres is reachable.
