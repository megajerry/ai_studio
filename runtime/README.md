# `runtime/` — event log + task queue (M1)

The coordination substrate every agent uses. Two Postgres-backed primitives:

- an **append-only event log** — the replayable source of truth for everything
  that happens (ADR-0004, ADR-0012);
- a **task queue** — how work is dispatched and how agents coordinate. Agents
  **never call each other**; "spawning an agent" means enqueuing a task
  (ADR-0009), and a worker claims it.

Everything is typed (pydantic), synchronous (psycopg 3), and small. Pure logic
lives in `models.py` and is unit-tested with no database.

## Layout

| File | Purpose |
| --- | --- |
| `models.py` | Enums, pydantic row models, `build_database_url`, `make_event`, `is_stale` (pure) |
| `db.py` | `connect()` / `can_connect()` (short-timeout probe) |
| `events.py` | `append_event`, `read_events` (insert + read only) |
| `tasks.py` | `enqueue_task`, `claim_task`, `heartbeat`, `complete_task`, `find_stale_tasks` |
| `migrate.py` | Forward-only migration runner (`python -m runtime.migrate`) |
| `migrations/*.sql` | Schema, applied in filename order |
| `tests/` | `test_models.py` (no DB) + `test_integration_db.py` (skips w/o DB) |

## Schema

### `events` (append-only)

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid pk | `gen_random_uuid()` |
| `ts` | timestamptz | `default now()` |
| `task_id` | uuid null | null for task-independent events |
| `workstream` | text | |
| `type` | text | e.g. `task.created`, `task.claimed`, `task.heartbeat`, `task.finished` |
| `payload` | jsonb | defaults `{}` |
| `trace_id` / `span_id` | text null | OpenTelemetry context (ADR-0012) |

Indexes: `(task_id, ts)`, `(workstream, ts)`. **No UPDATE/DELETE path** — the
data-access API only inserts and reads.

### `tasks` (queue)

| Column | Type | Notes |
| --- | --- | --- |
| `id` | uuid pk | |
| `workstream` | text | |
| `type` | text | |
| `status` | text | `queued \| in_progress \| blocked \| done \| failed` (CHECK) |
| `priority` | int | higher = more urgent |
| `assignee` | text null | `host \| offhost` (CHECK); null = any worker (ADR-0010) |
| `payload` | jsonb | enough state for a fresh agent to resume (ADR-0004) |
| `result` | jsonb null | |
| `heartbeat_at` | timestamptz null | liveness ping while `in_progress` |
| `claimed_by` | text null | worker id holding the claim |
| `budget_tokens` / `spent_tokens` | bigint | per-task cost cap + telemetry (ADR-0012) |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(status, priority DESC, created_at)` for claiming; partial
`(heartbeat_at) WHERE status='in_progress'` for the supervisor.

## API

All data-access functions take an open `psycopg.Connection` (the caller owns the
connection; each mutation runs in its own transaction, so the state change and
its event commit atomically).

```python
from runtime import connect, enqueue_task, claim_task, heartbeat, complete_task
from runtime import find_stale_tasks, append_event, read_events, make_event
from runtime.models import Assignee, TaskStatus

conn = connect()

t = enqueue_task(conn, workstream="productivity", type="build",
                 payload={"spec": "..."}, priority=5, assignee=Assignee.HOST,
                 budget_tokens=100_000)                     # emits task.created

claimed = claim_task(conn, worker_id="host-1", assignee=Assignee.HOST)
#   highest-priority queued task via SELECT ... FOR UPDATE SKIP LOCKED;
#   sets in_progress + claimed_by + heartbeat_at; emits task.claimed.
#   A worker gets tasks targeted at its assignee OR unassigned (null).
#   Returns None if nothing is claimable.

heartbeat(conn, t.id, "host-1")                              # emits task.heartbeat
complete_task(conn, t.id, result={"ok": True},
              status=TaskStatus.DONE, spent_tokens=1234)     # emits task.finished

read_events(conn, task_id=t.id)                              # replay a task
read_events(conn, workstream="productivity", since=some_ts) # scan a workstream
append_event(conn, make_event(workstream="productivity", type="note",
                              trace_id="...", span_id="..."))
```

Every task transition appends its event in the same transaction, so the log is a
complete, replayable record of the queue.

## Running migrations

```bash
export DATABASE_URL=postgresql://aistudio:...@localhost:5432/aistudio
# (or leave unset — it's assembled from POSTGRES_USER/DB/PASSWORD/HOST/PORT,
#  matching docker-compose.yml)

python -m runtime.migrate            # apply pending migrations   (make migrate)
python -m runtime.migrate --status   # list applied vs pending
```

Applied files are tracked in a `schema_migrations` table, so re-runs are
idempotent. Add a migration by dropping `migrations/NNNN_name.sql` with the next
number.

## How the supervisor uses `find_stale_tasks`

The non-agent supervisor (ADR-0004) is the irreducible liveness guarantee: "no
task is silently dropped." Its loop polls

```python
for task in find_stale_tasks(conn, threshold_seconds=SUPERVISOR_THRESHOLD):
    # heartbeat older than the threshold (or missing) while in_progress
    # → re-kick: reset to queued / spawn a fresh worker, and emit an event.
    ...
```

`find_stale_tasks` returns `in_progress` tasks whose `heartbeat_at` is older than
the threshold (nulls treated as stale), oldest-first. The equivalent pure
predicate `is_stale(task, threshold_seconds, now=...)` is exported for the
supervisor's own logic and is unit-tested without a DB.

## Tests

```bash
pip install -r runtime/requirements.txt pytest
pytest runtime/tests/                 # unit tests pass; DB tests skip w/o Postgres
```

`test_integration_db.py` probes the database with a short-timeout `can_connect()`
and **skips cleanly** (never hangs) when none is reachable — so it is safe in the
off-host sandbox. To exercise it: `docker compose up -d postgres && python -m
runtime.migrate && pytest runtime/tests/test_integration_db.py`.
