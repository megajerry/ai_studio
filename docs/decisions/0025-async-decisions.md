# 0025 — Async open-ended stakeholder decisions (park → free the worker → resume)

- **Status:** Accepted
- **Date:** 2026-07-26

## Context

The studio already has a first-class async loop for **binary** human-in-the-loop
gates: a 🔴 tool call the policy engine tiers `NEEDS_APPROVAL`
([ADR-0006](0006-comms-tiers.md), `runtime/approvals.py`). It never blocks a worker —
`runtime/enforce.invoke` pends the action, the dependent task is parked `blocked`
(`runtime.tasks.block_task`, `in_progress → blocked`), the worker immediately grabs
OTHER `up_for_grabs` work, and when a human approves/denies (later, whenever) the
worker/supervisor's `resume_approved` re-queues (`blocked → up_for_grabs`) or fails
the task. This is the shape ADR-0006 mandates: batched, async, human < 4 hrs/day.

But not every human input is approve/deny. A worker frequently hits an **open-ended
decision** — a real question with a chosen option or free-text answer: *"which
vendor?"*, *"what tone for the launch post?"*, *"pick A, B, or C."* Today there is
no primitive for that. The options are all bad:

1. **Block synchronously** — the worker sits idle waiting for a human who is not
   monitoring 24/7. Violates ADR-0006 (never block on the human) and wastes a worker.
2. **Force it into an approval** — approve/deny cannot express "choose option B" or
   free text; it is the wrong shape.
3. **Guess and proceed** — the agent fabricates a decision the stakeholder owns.

The binary approval loop already proves the correct pattern (park the task, free the
worker, resume on the async answer). We need the **open-ended analogue**.

## Decision

Add a first-class **async decision primitive** — the open-ended sibling of the
binary approval loop — that never blocks a worker:

    request_decision(dependent_task) → [open] + PARK the task (in_progress → blocked)
        → the worker is FREED: it grabs other up_for_grabs work (the blocked task is
          not grabbable), so nothing stalls waiting on the human
        → human answers later, whenever: answer_decision → [answered]
        → RESUME the parked task (blocked → up_for_grabs); a fresh worker re-grabs it
          and reads the chosen answer via get_decision.

**Distinct from a binary approval.** An approval resolves to `approved`/`denied` and
authorizes exactly one 🔴 tool execution (a one-shot grant bound to an action
fingerprint). A **decision** resolves to a *chosen option or free text* — there is no
grant and no tool fingerprint; the answer is data the resumed task reads and acts on.
The two are surfaced side by side but are separate stores and separate wire events.

**Reuses the existing lifecycle edges (no new task state).** Parking and resuming go
through the SINGLE guarded writer `runtime.tasks.transition` over the `blocked ↔
up_for_grabs` edges that already exist for approvals ([ADR-0015](0015-task-lifecycle-state-machine.md)) —
no ad-hoc status writes (invariant 4), no new lifecycle state. A decision-parked task
stamps `result = {blocked_on_decision, reason: awaiting_decision}`, kept DISTINCT from
the approval sweep's `blocked_on_approval` so `resume_approved` never touches it.

**Surfaced BATCHED by the Spokesman ([ADR-0006](0006-comms-tiers.md)).** A
`decision.requested` is classified 🛑 (needs a human reply, like an approval) and
batched into the periodic digest — NOT an immediate 🚨. The digest item renders the
question + options + default_choice; an inbound `decide <id> <answer>` command calls
`answer_decision`, mirroring the existing `approve/deny <id>` handling.

**Body-free events (invariants 5 & 6).** The question and answer TEXT live in the
`decisions` row only. `decision.requested` / `decision.answered` carry only id /
workstream / seq / status / has_options / resolver — never the question or answer
text on the wire, mirroring the `approval.*` / `trajectory.*` discipline. The
Spokesman composes human text by reading the decision's OWN row fields (leak-free).

**Storage.** A new `decisions` table (migration `0015_decisions.sql`): `id`,
`workstream`, `question`, `options` (jsonb, nullable — free text allowed when null),
`status` (`open|answered|withdrawn`), `answer`, `answered_by`, `dependent_task_id`
(FK → tasks, the parked task), `default_choice` (the safe default to note), `seq`
(monotonic), `created_at`, `answered_at`. Indexed on `status` and `workstream`.

**Scope.** This ADR delivers the primitive (`runtime/decisions.py`) + the Spokesman
channel only. Wiring the PM to auto-raise decisions is explicitly OUT of scope — a
follow-up can have the PM use it.

## Consequences

- A worker can surface a genuine open-ended question to the stakeholder without ever
  stalling: the task parks, the worker keeps working, the answer resumes it later.
- The stakeholder answers on their own async cadence (ADR-0006), one batched digest
  reply (`decide <id> <answer>`), never an interrupt.
- No new task state and no new guarded writer — the existing `blocked ↔ up_for_grabs`
  recovery edges and `runtime.tasks.transition` carry it.
- `withdraw_decision` closes a no-longer-needed decision and still resumes the parked
  task (which reads the `withdrawn` status and can fall back to `default_choice`), so
  a parked task is never orphaned.
- A decision does NOT authorize a 🔴 action — a task that also needs a 🔴 tool call
  still goes through the approval grant loop. Decisions and approvals compose.

## Cross-references

- [ADR-0006](0006-comms-tiers.md) — comms tiers; batched/async stakeholder comms.
- [ADR-0015](0015-task-lifecycle-state-machine.md) — the canonical task lifecycle +
  the `blocked ↔ up_for_grabs` edges this reuses.
- `runtime/approvals.py` — the binary sibling loop this mirrors.
