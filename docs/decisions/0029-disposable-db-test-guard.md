# 0029 — Keep the live DB sacred: DB-backed tests + demo may only touch a disposable DB

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

The DB-backed test suite (`runtime/tests/*_db.py`, `spokesman/tests`, `evals/tests`)
achieves isolation by seeding rows under **unique random workstreams** and then
**never tearing them down** — by design there is no teardown, tests just don't
collide. The synthetic data is verbatim `work.a` / `work.b` / `work.demo` task types
and `pollute-0..49` workstreams (e.g. `enqueue_task(conn, workstream=ws,
type="work.demo", ...)`). `runtime/demo.py` likewise enqueues `work.demo` tasks and
did not clean up.

The only gate protecting a database was `runtime/db.py::can_connect`: tests skip when
Postgres is **unreachable** (off-host / no docker). Nothing distinguished a *throwaway*
database from the **live studio DB**. So running `make test`, `python -m runtime.demo`,
or the readiness check against the real `DATABASE_URL` permanently polluted the task
queue — measured at **76,967 synthetic tasks** on one box. The live queue is the
studio's source of truth; polluting it corrupts capacity, planning, and rollups.

## Decision

**The production database is sacred. DB-backed tests refuse to run against a
non-disposable database, and `runtime.demo` self-cleans.**

1. **A single guard** — `runtime/db_guard.py::require_disposable_db()` returns
   `(is_disposable, reason)`. A database is **disposable** iff:
   - env `AI_STUDIO_TEST_DB` is truthy (explicit operator opt-in), **or**
   - the target database *name* **ends with `_test`** (case-insensitive). This is a
     strict suffix, NOT a loose "contains `test`" substring — otherwise production-ish
     names like `attestation` / `latest` / `contest` would be silently treated as
     throwaway. When in doubt, the opt-in env var is the explicit escape hatch.

   The name is extracted robustly from the resolved dsn (URL form incl. query params,
   or libpq `dbname=` keyword form; falls back to `POSTGRES_*` via
   `build_database_url`). The reason string names only the DB, never a credential
   (invariant 5).

2. **A shared collection-time gate** — the repo-root `conftest.py` hook
   `pytest_collection_modifyitems`: if a DB is reachable but NOT disposable and there
   is no opt-in, **every DB-backed item is skipped LOUDLY** with the guard reason
   (`"refusing to run DB-backed tests against non-disposable DB '<name>'; set
   AI_STUDIO_TEST_DB=1 or use a *_test database"`). The skip is visible in pytest
   output, so "all skipped" is never mistaken for "all passed". Behavior matrix:
   no DB → skip (unchanged, per-module `can_connect` skipif); reachable + not
   disposable + no opt-in → skip with the guard reason; disposable → run normally.

   *Design note (for reviewers):* there is no single shared `conn` fixture to gate —
   each of the ~48 DB test modules defines its own `conn`/`live_conn` fixture or
   connects inline, all self-gating on `can_connect`. The lower-risk structural
   chokepoint is therefore **collection** (one repo-root conftest) rather than editing
   48 modules. A DB-backed item is detected structurally: it requests a connection
   fixture (`conn`/`live_conn`/`setup_conn`) OR its module CALLS `can_connect(`
   (exactly how every DB suite gates — matching the call form, not a bare mention,
   so the guard's own unit tests are not self-flagged).

   *CI note:* a bare `pytest` (not `make test`) against a reachable non-disposable DB
   skips the whole DB suite yet exits 0 — an all-skipped run must not read as green.
   CI therefore runs `make test` (which sets the opt-in) or asserts zero unexpected
   skips.

3. **Demo self-containment** — `runtime.demo` tracks the exact rows it creates and, in
   `main()`'s `finally`, deletes **only those** (`_cleanup_workstreams`). It introspects
   `information_schema` for every workstream-scoped table and deletes by exact
   workstream, plus rows that have NO workstream column, tracked by exact id/key as the
   demo creates them:
   - child rows tied to its own tasks (`task_transitions`; `trajectory_steps` cascade);
   - `approvals` — both those reachable via a demo task's `task_id` **and** the two
     parentless ones the demo raises directly: the reviewer `review` approval (whose
     `task_id` points at a synthetic Task never inserted) and the experiment
     `experiment.scale` approval (`task_id IS NULL`), tracked by exact `approvals.id`;
   - `search_cache` — the researcher step's cached row(s), tracked by exact
     (`query_hash`,`provider`,`k`) key via a tight before/after snapshot.

   FK-safe order: `spokesman_handoffs` (its `approval_id → approvals` is `NO ACTION`)
   before `approvals`; `tasks` deleted LAST. **Never** a global `TRUNCATE`/`DELETE`,
   never a `LIKE` pattern, never a row it did not create. Verified: two consecutive
   `python -m runtime.demo` runs leave **zero net rows in EVERY table** (incl.
   `approvals` / `search_cache`), preserving keyless/dry-run/exit-0 semantics.

4. **Sanctioned invocation** — the `Makefile` `test` / `coverage` / `evals` targets set
   `AI_STUDIO_TEST_DB=1` inline (scoped to those targets only; never exported), so the
   intended dev/CI flow works. It is documented (commented) in `.env.example` with an
   explicit "leave blank on any host that runs the real studio" warning.

## Consequences

- Running the suite against the live DB no longer pollutes it — it skips with a loud,
  actionable reason instead. The measured 76,967-task pollution class of bug is
  structurally prevented.
- The sanctioned path is unchanged in spirit: `make test` on a throwaway/`*_test` DB
  runs green; a fresh `*_test` database needs no opt-in at all.
- `python -m runtime.demo` remains the safe on-host go-live smoke test (self-cleans),
  and `runtime.readiness::check_demo` still passes.
- The guard is name/opt-in based (not a live-vs-test connection probe), so it is
  deterministic, offline, and cannot itself touch the DB.

## Cross-references

- `runtime/db_guard.py` — the guard; `conftest.py` — the shared collection gate.
- `runtime/demo.py` — `_new_ws` / `_cleanup_workstreams` self-cleanup.
- [ADR-0015](0015-task-lifecycle-state-machine.md) — the task queue this protects.
- [ADR-0011](0011-secrets-and-onboarding.md) — no secrets in guard reasons.
