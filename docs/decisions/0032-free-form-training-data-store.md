# 0032 — Free-form training-data store (relocate embedded event free-text)

- **Status:** Accepted
- **Date:** 2026-07-27

## Context

The append-only event log is documented as **body-free** (invariant #6, ADR-0011 /
ADR-0012 / ADR-0020): payloads carry only ids / types / counts, never free-form
prose, so the public-repo log holds no PII/secret/model bodies and the bodies of
richer reasoning (trajectories) live in the **local DB only** (invariant #7,
"LOCAL DB ONLY").

An audit found that several emit sites nonetheless embedded **free-form text**
directly in `events.payload`:

- **`runtime/roles/pm.py`** — `pm.pushback` / `pm.needs_clarification` /
  `pm.consensus` / `pm.planned` emitted `payload={"goal": goal, …}` (the verbatim
  free-text objective) and, on pushback / needs-clarification, a free-text
  `"reason"`.
- **`runtime/roles/verifier.py`** — `verify.passed` / `verify.failed` emitted
  `payload={"reason": verdict.reason}` — **model-authored** verifier prose.
- **`runtime/worker.py`** — `work.retry` emitted `payload={"reason": verdict.reason}`
  — the same model-authored verifier prose.

The stakeholder direction (2026-07-27) is explicit: **do not redact** this text —
it is valuable **self-improvement training data** — but it must not live on the
body-free event log. It is to be **relocated** into a dedicated free-form field/store,
local-DB-only, mirroring how trajectory bodies are handled (ADR-0020).

## Decision

Add a dedicated **free-form training-data store** and route the audited free-text
into it, keeping the event log body-free.

### Schema (migration `0019_event_free_form.sql`)

> **Numbering note.** The store is migration **0019** (the next free contiguous
> migration number — migrations ran `0001..0018`, and `runtime/readiness.py`
> enforces a contiguous `0001..000N` sequence, so `0032` would fail that check).
> The **ADR** is `0032` (the next free ADR number). The two sequences are
> independent; the task brief's "migration 0032" referred to the ADR number.

`event_free_form` — one relocated free-form string:

- `id`, `created_at`;
- **link keys** back to the originating event: `event_type` (always), `workstream`
  (always), `task_id?`, `trajectory_id?`, and `event_seq?` (nullable — the
  `EventSink` write path does not surface the assigned events `seq`; retained for
  callers that can supply it);
- `kind ∈ {goal, reason, rationale}` (CHECK-constrained closed vocabulary):
  `goal` = a PM objective / restated goal; `reason` = a PM plan reason (pushback /
  needs-clarification); `rationale` = model-authored verifier prose (`verify.*` /
  `work.retry` `verdict.reason`);
- `content` — the **full free-form text**, LOCAL DB ONLY, never on the wire.

Indexes for retrieval-as-training-data: `(kind, created_at)`, `(task_id)`,
`(event_type)`. **No FKs** to `tasks` / `trajectories` on purpose — training data
must outlive both a task deletion and a trajectory TTL expiry, so links are plain
indexed correlation columns, not cascading references.

### Single guarded writer/reader (`runtime/free_form.py`)

All writes go through **`record_free_form(conn, *, kind, content, event_type,
workstream, task_id?, trajectory_id?, event_seq?)`** — parameterized SQL only, no
ad-hoc INSERTs elsewhere (the `runtime.trajectory` / `runtime.tasks.transition`
discipline). It is **observe-only and degrade-safe** (ADR-0017): with no `conn`
(unit/fake-queue paths), blank `content`, an unknown `kind`, or on any write
failure it logs and returns `None` — it **never raises**, so relocating training
data can never break the caller's event emit. `read_free_form(conn, *, kind?,
task_id?, event_type?, workstream?, limit?)` is the retrieval reader.

### Emit sites: payload stays body-free, text goes to the store

At each audited site the free-text is dropped from the payload and written to the
store, linked to the task + event type (emit order is irrelevant to resolvability
because linkage is by `task_id + event_type`, not the un-surfaced `seq`):

- `pm.pushback` / `pm.needs_clarification` → payload keeps `confidence`
  (+ `threshold` for needs-clarification); `goal` + `reason` → store.
- `pm.consensus` → payload keeps `rounds` / `outcome` / `concern_count`; `goal` → store.
- `pm.planned` → payload keeps `confidence` / `work_item_count` / `work_task_ids`
  (ids, not prose); `goal` → store.
- `verify.passed` / `verify.failed` → payload keeps the `passed` flag; the model
  `verdict.reason` → store as `rationale`.
- `work.retry` → payload keeps `attempt`; the model `verdict.reason` → store as
  `rationale`.

### The approvals.reason exception (retained, documented)

`runtime/approvals.py`'s short `reason` on `approval.requested` is **DOCUMENTED as
intentional and bounded** (ADR-0006) and is **left in place**. The Spokesman digest
(`spokesman/runtime_bridge.py`) composes the human-facing approval / alarm / flag
text from that bounded `reason` (and from `review.alarm` / `review.flagged`
reasons — none of which are audited sites), so relocating it would break the
stakeholder feed. This is the one deliberate, bounded free-text field that remains
on the wire; the store is for the **unbounded** objectives/rationale above.

### Consumer coupling (checked before removing)

Every reader of the audited events was checked. None read the relocated free-text:
`runtime/quality.py` and `runtime/roles/{retro,reviewer}.py` / `runtime/adaptive.py`
only **count** `verify.passed` / `verify.failed` / `work.retry` occurrences (and read
unrelated payload keys like `to` / `stall_reason` / `error_type`); the Spokesman
reads only the bounded `approvals.reason` (retained). So removing `goal` / `reason`
from the audited payloads breaks no consumer. The only test asserting an audited
payload shape (`pm.consensus`) was updated to the body-free key set.

## Consequences

- The event log is now genuinely body-free at every audited site — a
  `events.payload::text` scan finds none of the relocated objectives/rationale
  (proven by a live-DB sentinel test), reconciling the invariant #6 documentation
  with reality.
- The free-text is **retained, not lost** — it accrues in `event_free_form` as
  labeled training data (`kind`), retrievable per task / per kind for the learning
  agents, and durable across task/trajectory deletion.
- One bounded, documented exception (`approvals.reason`) intentionally remains on
  the wire because a consumer needs it; it is called out here so it is not mistaken
  for a regression.
- A new persistence surface adds a migration + a writer module; the writer is
  degrade-safe and observe-only, so it carries no behavioral risk to the running
  loop.

## References

- [ADR-0006](0006-stakeholder-comms.md) — the bounded `approvals.reason` the
  Spokesman digest consumes; the one intentional free-text field retained on the wire.
- [ADR-0011](0011-secrets-and-onboarding.md) — public-repo / no-PII discipline; bodies
  live in a git-ignored local surface, never on the wire.
- [ADR-0012](0012-telemetry-metrics.md) — append-only, body-free telemetry the event
  log already keeps.
- [ADR-0017](0017-db-resilience-and-remote-access.md) — degrade-rather-than-crash; the
  free-form writer no-ops on any failure so an emit is never load-bearing on it.
- [ADR-0020](0020-trajectory-observability.md) — the "bodies are LOCAL DB ONLY,
  events stay body-free" pattern this store mirrors (single guarded writer, local
  bodies, body-free events).
