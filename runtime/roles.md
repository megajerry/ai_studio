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
| `roles/retro.py` | `run_retro` — distill 1-3 durable **lessons** from an episode's trail into Knowledge memory; emits `retro.completed` (count only) |
| `roles/lessons.py` | `inject_lessons`/`compose_lessons` — auto-inject the recalled lessons into a role's prompt (`### Lessons`), the deterministic apply-the-lesson step |
| `worker.py` | `run_once` (claim → dispatch → heartbeat → verify→commit; triggers Retro on terminal work), `run()`/`main()` |
| `demo.py` | `python -m runtime.demo` — end-to-end proof + learning-loop proof against a live DB (skips w/o DB) |
| `tests/test_roles.py` | role units + policy-gate refusals (🔴 delete/shell) — keyless, no DB |
| `tests/test_worker.py` | full loop via an in-memory fake queue; verify-fail re-enqueue |
| `tests/test_retro.py` | lesson distillation + retro trigger policy + NO retro-loop; live-DB run_retro |
| `tests/test_lessons.py` | lesson injection: bounded/scoped, behavior-preserving with no lessons |

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

## The learning loop (ADR-0003)

The studio *learns from mistakes over time* structurally, not by hoping the model
remembers. A finished episode's lessons are distilled once, stored durably, and
**auto-injected** into future work:

```
work.* finishes (done|failed)
        │  worker enqueues a `retro` task  (WORKER_RETRO=on_fail|always|off)
        ▼  worker.run_once (claim retro)   [a retro NEVER enqueues another → no loop]
   Retro: read the episode's event trail (read_events, deterministic seq order)
          call_model(role=retro, task_type=retro)   ── traceability only (dry-run)
          distill 1-3 concise lessons (bounded; failures → prevention lesson)
          memory.add_lesson(...) → Knowledge layer   ──emits──> memory.remembered (id/dims only)
          ──emits──> retro.completed (lesson COUNT + task ref — NEVER the lesson text)
        │
        ▼  NEXT time any PM/Executor acts in this workstream
   recall_lessons(conn, workstream, query, k) → inject_lessons(prompt, …)
          bounded, delimited `### Lessons` section prepended to the role's prompt
          (workstream-scoped + shared global corpus; ADR-0013 bounded injection)
```

**Design properties (ADR-0003):**

- **Prompt-level prevention > runtime correction.** Applying a lesson is the
  deterministic `inject_lessons` step at prompt assembly — it does not depend on
  the model recalling anything.
- **Cross-episode accumulation > single-pass reflection.** Lessons persist in the
  Knowledge layer and compound across episodes; there is **no reflection loop**
  (≤ `MAX_LESSONS=3` per retro, single pass over the trail).
- **Adaptive intensity (adaptive-lite).** `WORKER_RETRO=on_fail` (default) runs a
  retro only on a failed episode — "more retro when the error rate is high" — at
  minimal token cost; `always` retros every episode; `off` disables the trigger.
- **No leakage.** `retro.completed` and the memory events carry only counts/ids;
  the lesson text lives in the Knowledge layer, never on the event log.
- **Behavior-preserving.** With no lessons (or no `conn`), `inject_lessons` returns
  the base prompt unchanged — the roles behave exactly as before.

### Roles & least privilege (policy.example.yaml)

| Role | Granted capabilities | Why |
| --- | --- | --- |
| `pm` | `fs.read` | plans + calls a model; never does the work |
| `executor` | `fs.read`, `fs.write` | writes the scratch artifact (🟡). **No `fs.delete`/`shell.exec`** → those DENY |
| `verifier` | `fs.read` | read-only independent check |
| `retro` | *(none)* | reads the event trail + calls a model; writes lessons to Knowledge memory (not a host tool). No tool capabilities needed |

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
the event trail, then a **second act** demonstrating the learning loop: a work
task fails → Retro distills a lesson → the next PM prompt for that workstream is
shown to include the recalled `### Lessons` section (prints `lesson learned: N`).
With **no** database it prints a notice and exits 0 (deferred to host
verification) — it never hangs.

## Config (env)

- `WORKER_ID` — stable worker identity (default `worker-<rand>`).
- `WORKER_SCRATCH_DIR` — the FilesystemTool root (default a temp dir).
- `WORKER_IDLE_SLEEP_S` — poll gap when the queue is empty (default 5s).
- `WORKER_MAX_WORK_ATTEMPTS` — verify-fail re-enqueues before failing (default 2).
- `WORKER_RETRO` — when a terminal work task triggers a Retro: `on_fail`
  (default) | `always` | `off` (the learning-loop trigger; adaptive-lite).
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
