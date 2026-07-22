# `runtime/` M3c — roles + worker (the studio operates end-to-end)

The minimal set of roles + the on-demand worker that make the studio **operate
end-to-end in dry-run** (no API keys, no Docker). This is the first time the merged
substrate runs as one loop: task queue + event log (M1), the policy-gated tool
path (M2), and the single instrumented model call (M3b), driven by three roles.

Everything here obeys the CLAUDE.md invariants: agents don't call agents
(coordination is via the queue/events), agents don't touch the host (side effects
only through a tool via `invoke`), mutations go through **verify → commit**, and
every action emits an event. It runs **fully keyless** — `call_model` falls back
to the dry-run provider when no key is present.

## Layout

| File | Purpose |
| --- | --- |
| `roles/pm.py` | `run_pm_tick` — plan + confidence gate, enqueue ONE work task; emits `pm.planned` |
| `roles/executor.py` | `run_executor` — DO the work: a policy-gated `filesystem` write + a `call_model` dry-run call |
| `roles/verifier.py` | `verify` — INDEPENDENT verify→commit gate (read-only); returns pass/fail |
| `worker.py` | `run_once` (claim → dispatch → heartbeat → verify→commit), `run()`/`main()` |
| `demo.py` | `python -m runtime.demo` — end-to-end proof against a live DB (skips w/o DB) |
| `tests/test_roles.py` | role units + policy-gate refusals (🔴 delete/shell) — keyless, no DB |
| `tests/test_worker.py` | full loop via an in-memory fake queue; verify-fail re-enqueue |

> A role is `prompt + skills + tools` (architecture §3). Today the prompt is an
> **inline string template** on each role and the "skills" layer (Agent Skills
> standard, ADR-0008) is deferred to a later milestone. Roles act on the world
> only through `invoke` (tools) and `call_model` (models).

## The operating loop (architecture §4, ADR-0004/0009)

```
scheduler.tick_once ──enqueue──> pm.tick
        │
        ▼  worker.run_once (claim pm.tick)
   PM: confidence gate (call_model role=pm task_type=plan)  ──emits──> model.routed, model.call
       define success criterion + marker
       enqueue ONE work.demo task (payload: goal, criterion, marker)  ──emits──> pm.planned, task.created
        │
        ▼  worker.run_once (claim work.demo)   [heartbeats around each phase]
   Executor: call_model(role=exec task_type=execute)         ──emits──> model.routed, model.call
             invoke(role=executor, filesystem, op=write ...)  ──emits──> policy.decision, tool.invoked
   Verifier: call_model(role=verifier task_type=verify)      ──emits──> model.routed, model.call
             invoke(role=verifier, filesystem, op=read ...)    ──emits──> policy.decision, tool.invoked
             deterministic check: marker present in artifact? ──emits──> verify.passed | verify.failed
        │
        ├─ pass → complete_task(status=done)                  ──emits──> task.finished   ← verify→commit
        └─ fail → complete_task(status=failed) + bounded re-enqueue (attempt+1)
                                                              ──emits──> task.finished, work.retry
```

**verify → commit is enforced**: a `work.*` task never becomes `done` until the
Verifier returns `passed`. The Verifier is a *separate* role, granted only
`fs.read`, so it can inspect but never "fix" the work it judges (independence).

### Roles & least privilege (policy.example.yaml)

| Role | Granted capabilities | Why |
| --- | --- | --- |
| `pm` | `fs.read` | plans + calls a model; never does the work |
| `executor` | `fs.read`, `fs.write` | writes the scratch artifact (🟡). **No `fs.delete`/`shell.exec`** → those DENY |
| `verifier` | `fs.read` | read-only independent check |

A 🔴 tool from a role that lacks the capability → **DENY** (e.g. executor→delete);
a 🔴 tool from a role that *has* it → **NEEDS_APPROVAL** (never auto-executes).
Both are asserted in the tests.

## Worker

`run_once(conn, worker_id, sink, *, registry, config, …)` is the single testable
unit:

1. `claim_task` (M1) — highest-priority queued task, or `None` (caller idles).
2. dispatch by `task.type`: `pm.tick` → PM; `work.*` → Executor then Verifier.
3. `heartbeat` around each work phase (liveness is the **worker's** job, not the
   role's — the supervisor re-kicks a task whose heartbeat goes stale, M3a).
4. verify pass → `complete_task(done)`; fail → bounded re-enqueue (`work.retry`,
   `attempt+1`) up to `WORKER_MAX_WORK_ATTEMPTS`, else `complete_task(failed)`.

Every seam (`claim`/`heartbeat`/`complete`/`enqueue` + the three role handlers) is
injectable, so the whole loop is driven in tests with an in-memory fake queue and
no database.

`run()` + `main()` are the on-demand driver: claim + service tasks, sleeping
`WORKER_IDLE_SLEEP_S` only when the queue is empty; reconnects on a dropped
connection and never lets one bad task kill the driver (mirrors the
supervisor/scheduler). `python -m runtime.worker`.

### How it fits with supervisor + scheduler (M3a)

- **scheduler** (`runtime.scheduler`) enqueues the `pm.tick` pulse without pileup.
- **worker** (this milestone) materializes on demand to claim + service any task.
- **supervisor** (`runtime.supervisor`) is the liveness backstop: it re-kicks
  in-progress tasks whose heartbeat went stale and force-fails ones that exhaust
  their retries. The worker's heartbeats are what keep a healthy task off the
  supervisor's radar.

All three are **non-LLM** drivers; the reasoning lives only in the roles, and even
there only through `call_model`.

## Demo

```bash
python -m runtime.demo      # forces MODELS_DRY_RUN; needs Postgres
```

Runs tick → PM → work → Executor + Verifier → done against a real DB and prints
the event trail. With **no** database it prints a notice and exits 0 (deferred to
host verification) — it never hangs.

## Config (env)

- `WORKER_ID` — stable worker identity (default `worker-<rand>`).
- `WORKER_SCRATCH_DIR` — the FilesystemTool root (default a temp dir).
- `WORKER_IDLE_SLEEP_S` — poll gap when the queue is empty (default 5s).
- `WORKER_MAX_WORK_ATTEMPTS` — verify-fail re-enqueues before failing (default 2).
- `MODELS_DRY_RUN=1` — force keyless dry-run (the demo sets this).

## Verify

```bash
pip install -r runtime/requirements.txt pytest
python -m py_compile runtime/*.py runtime/roles/*.py
pytest runtime/tests/                 # full loop keyless; DB e2e skips w/o Postgres
python -m runtime.demo                # end-to-end on the host (skips w/o DB)
```

No network, no keys, no Docker required for the tests: the loop runs on the
dry-run provider with a temp-dir FilesystemTool and a `MemoryEventSink`/fake queue;
the DB end-to-end test (`test_integration_db.py::test_worker_full_loop_pm_to_done`)
skips cleanly when no Postgres is reachable.
