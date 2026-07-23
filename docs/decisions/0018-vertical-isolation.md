# 0018 — Vertical isolation: where a vertical's four kinds of state live

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

The studio runs many **vertical** workstreams (a video channel, a game, a
product) on top of the one horizontal Productivity platform (ADR-0002). For the
platform to safely run several verticals at once — and to keep each vertical
disposable (kill a loser without collateral, ADR-0016) — we need an explicit rule
for **where each vertical's state lives and how it is isolated** from the others
and from the platform itself.

The role-customization seams already exist and are verified: the shared role
prompt assembler takes a workstream charter + per-role overlay
(`runtime/roles/prompt.py`), and the Verifier dispatches a structured criterion to
a pluggable domain checker (`runtime/roles/checkers.py`). What remained was (a) a
**config/registration record** that drives those seams so a vertical is defined by
data, not code, and (b) ratifying the **isolation model** the record assumes. This
ADR ratifies the isolation model; the record is
`runtime/workstream/` + `workstreams/<name>/config.yaml` (see below). The
**cross-workstream request contract** (a typed `feature_request` + receiving-PM
intake/triage) is a separate follow-up (state/backlog.md item 1), not part of this
ADR.

## Decision

A vertical has **four kinds of state**, each with a distinct home and isolation
boundary:

### 1. State → the platform Postgres, scoped by `workstream`

All operational state — tasks, events, approvals, memory, budgets, experiments —
lives in the platform's Postgres and is **scoped by the `workstream` column**, not
a separate database per vertical. Isolation is enforced in code, not by
infrastructure:

- **Memory** reads *by scope* — a recall is addressed to one layer within one
  workstream and cannot cross a workstream boundary (`runtime/memory/`,
  `scope_where`/`in_scope`, defense-in-depth SQL + Python predicate). Workstream A
  never sees workstream B's Knowledge lessons; a deliberately-shared global corpus
  (`'*'`) is the only exception.
- **Budget** is per-`(workstream, period)` (`budgets` table); enforcement sums a
  workstream's own `model.call` spend only (`runtime/budget.py`).
- **Tasks/events/approvals** all carry `workstream`; reads filter on it.

Rationale: the platform is local-first and single-Postgres (ADR-0007); a
column-scoped model keeps cross-workstream *observability for the platform*
(ADR-0002 — Productivity may optimize across verticals by reading their logs)
while code-level scope rules keep verticals from reading each other. A
database-per-vertical would break that platform-level read and add ops weight for
no isolation we don't already get in code.

### 2. Artifacts → an object store, **one bucket per workstream**

Produced artifacts (renders, builds, exports — anything larger than a row) go to
an S3-compatible object store (MinIO in phase 1, per the stack), with **one bucket
per workstream** named in the config (`object_store_bucket`). The bucket name is
config; credentials are provisioned separately into the git-ignored env (ADR-0011)
and reached only through a tool (invariant 5) — never embedded in the config and
never handed to an agent. Isolation is the bucket boundary + the vertical's
scoped credentials.

### 3. Product code → its own repo, built by the coding worker

A vertical's **product** (the game, the site, the pipeline) is NOT committed to
this platform repo. It lives in **its own repository**, built and modified by the
coding worker (opencode) inside the sandbox via the `code.run` 🔴 path
(architecture §14, the existing `CodingTool`). This keeps the platform repo purely
horizontal (ADR-0002) and lets a vertical's product be killed/forked/handed off
independently.

### 4. Definition → the platform repo, `workstreams/<name>/`

The vertical's **definition** — its charter, per-role overlays, budget,
policy grants, skill set, domain checkers, memory seed, and bucket name — lives in
**this repo** as `workstreams/<name>/config.yaml`, loaded + validated by
`runtime/workstream/config.py` (`WorkstreamConfig`). This is the "config, not
code" record: it drives the existing seams (charter/overlay → the role prompt
assembler; checkers → the Verifier; policy grants → merged over the base policy;
budget/memory-seed → `bootstrap_workstream`) so **standing up a vertical writes no
role code**. The config is strictly rules-as-data and contains **no secrets**
(ADR-0011): it only *names* a bucket / references skills + checkers by name.

## Consequences

- A vertical is defined by one committed YAML file + out-of-band keys + a product
  repo; the platform runs it through the same PM → Executor → Verifier → Retro
  loop, learning, telemetry, and approvals as every other workstream.
- Isolation is **code-scoped, not infra-scoped**, for state (one Postgres,
  `workstream` column + scope rules), and **infra-scoped** for artifacts (bucket
  per workstream) and product (repo per vertical).
- The platform keeps its cross-vertical read (ADR-0002) while verticals cannot
  read each other's memory/budget/tasks — proven by the memory scope tests and the
  workstream-config isolation tests.
- Killing a vertical (ADR-0016) is bounded: drop its config, stop seeding/among
  the loop, and its bucket + product repo are independently disposable; its rows
  remain in the log for replay/audit.
- **Out of scope (follow-up):** the cross-workstream request contract (typed
  `feature_request` + receiving-PM intake/triage/prioritize/approve/decompose +
  symmetric escalation) — tracked in state/backlog.md item 1.

See [`workstreams/README.md`](../../workstreams/README.md) for how to start a
vertical end-to-end and [`runtime/workstream/`](../../runtime/workstream/) for the
config schema + the seams it drives.
