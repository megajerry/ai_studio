# `runtime/approvals` — the human-in-the-loop grant loop for 🔴 actions

The policy engine already decides that a 🔴 (delete / spend / deploy / shell) or
over-budget call is `NEEDS_APPROVAL` (architecture §5, CLAUDE.md approval tiers).
What was missing was a way to actually **grant** that approval and let the action
proceed. This module is that loop: a durable approval store + the wiring that
turns a human "yes" into exactly one authorized execution.

It is the runtime half of ADR-0006's 🛑 **"Approve (blocks)"** class — the
Spokesman/WhatsApp surface that renders and resolves these is a **separate, later
task**. Here we only provide the persistence, the state machine, and the enforced
execution path; `pending_approvals` / `pending_digest` are the read side the
future Spokesman will batch into its periodic digest.

## The flow

```
invoke(🔴, conn)  ──no grant──▶  request_approval  ──▶  [pending]
                                                          │
                          human: resolve_approval(approved)│
                                                          ▼
worker: resume_approved  ◀── re-queue ──  [approved]  (a one-shot grant)
        │
        ▼ (worker re-claims the same task, re-runs the action)
invoke(🔴, conn)  ──find_grant hit──▶  execute tool  ──▶  consume_grant  ──▶  [consumed]

        deny path:  resolve_approval(denied) ──▶ [denied] ──▶ resume_approved fails the task
```

1. **pend.** An agent calls `enforce.invoke(...)` for a 🔴 action, passing `conn`.
   With no live grant, `invoke` calls `request_approval` — a durable `pending`
   row — and returns `PENDING` **without executing**. It emits `approval.requested`.
2. **park.** The worker sees the executor's `PENDING` and calls `block_task`: the
   task goes `blocked` with the `approval_id` stored in its `result`. The worker
   **stops** — it does not verify or complete. No new event beyond the
   `approval.requested` invoke already emitted (the block is traceable via that
   event's `task_id`).
3. **resolve.** A human (later, via the Spokesman) calls
   `resolve_approval(approval_id, "approved"|"denied", resolver)`. It flips the
   `pending` row and emits `approval.resolved`. An **approved** row is now a
   one-shot **grant**; a **denied** row stays denied.
4. **resume.** `resume_approved(conn, sink)` (hooked into the worker loop) scans
   `blocked` tasks:
   - approval **approved** → `requeue_blocked_task` sets the task back to
     `queued` (emits `approval.resumed`); a worker re-claims it.
   - approval **denied** → `complete_task(failed)` — the 🔴 action was refused,
     the task fails, the tool never runs.
5. **execute + consume.** On the retry, the executor calls `invoke` again for the
   **same** task+tool+capabilities. Its `request_fingerprint` matches, so
   `find_grant` returns the grant, `invoke` **executes the tool**, calls
   `consume_grant` (row → `consumed`), and emits `tool.invoked` carrying the
   `approval_id`. The Verifier then runs as usual → commit `done`.

## One-shot, by construction

A grant authorizes **exactly one** execution. `consume_grant` is guarded to
`status = 'approved'`, so:

- a second identical call finds **no** grant (the row is now `consumed`) and pends
  afresh — a brand-new `pending` request, not a reuse of the spent one;
- two workers racing to consume the same grant: only one `UPDATE ... WHERE
  status='approved'` matches; the loser falls through and pends.

`invoke` consumes the grant **before** the side effect, so a crash mid-execution
still spends the grant — "one grant = one execution attempt", never a free retry.

## The fingerprint

`compute_fingerprint(task_id, tool, capabilities)` is a stable SHA-256 of
`task_id | tool | sorted(capabilities)`. It is how the *first* (pending)
invocation and the *later* (post-approval) retry of the same action collide, so
the grant re-attaches to the retry. Because `resume_approved` re-queues the
**same** task row (same `task_id`), the fingerprint is identical across the pend
and the retry. It carries **no argument values or secrets** (invariant 5) — only
the action's identity.

## Events (no secrets, ever)

| Event | When | Payload (identity only) |
| --- | --- | --- |
| `approval.requested` | a 🔴 call pends | `approval_id, task_id, role, tool, tier, reason, capabilities` |
| `approval.resolved` | human approves/denies | `+ status, resolver` |
| `approval.resumed` | a granted task is re-queued | `approval_id, status` |
| `tool.invoked` | a grant authorized a run | `+ approval_id` |

Payloads carry the action's **identity** (role, tool, tier, capability *names*)
for auditing — never argument values, file contents, or secrets. Capabilities are
the policy vocabulary strings (`fs.delete`, `spend.money`, …), not data.

## API (`runtime/approvals.py`)

- `request_approval(conn, *, task_id, role, tool, capabilities, tier, reason, sink, workstream, fingerprint=None)`
  → create a `pending` row (**idempotent per fingerprint**: an existing
  `pending`/`approved` row is returned as-is, no duplicate, no re-emit) + emit
  `approval.requested`.
- `resolve_approval(conn, approval_id, decision, resolver, sink, workstream=...)`
  → flip a `pending` row to `approved`/`denied` (guarded to `pending`, so an
  already-resolved one is never re-decided) + emit `approval.resolved`.
- `find_grant(conn, fingerprint)` → an `approved`, un-consumed grant, or `None`.
- `consume_grant(conn, approval_id)` → mark `consumed` (one-shot; guarded to
  `approved`).
- `get_approval(conn, approval_id)` → fetch one row by id (any status).
- `pending_approvals(conn)` / `pending_digest(conn)` → the read side for the
  future Spokesman (a flat list / a tier-grouped `ApprovalDigest`).
- `compute_fingerprint(task_id, tool, capabilities)` → pure, DB-free.

Task-state helpers live in `runtime/tasks.py` (`block_task`,
`requeue_blocked_task`, `find_blocked_tasks`); the resume driver
(`resume_approved`, `ResumeResult`) lives in `runtime/worker.py`.

## Schema (`migrations/0007_approvals.sql`)

`approvals`: `id uuid pk, task_id uuid, role, tool, capabilities text[], tier,
reason, request_fingerprint, status CHECK(pending|approved|denied|consumed),
created_at, resolved_at, resolver`. Indexed on `(status)` (digest/queue path) and
`(request_fingerprint)` (grant-matching path). Forward-only + idempotent
(`CREATE ... IF NOT EXISTS`).

## Where the gate holds

- The `conn=None` path of `invoke` is unchanged — it pends **ephemerally** with no
  persistence, so pure unit tests stay DB-free.
- A 🔴 action **cannot** execute without an explicit, human-approved, un-consumed
  grant matching its fingerprint. There is no auto-approve and no bypass of the
  policy gate — `invoke` is still the only path to a tool's `execute`.
