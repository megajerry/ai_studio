# Retro: Cursor Task is not the studio PM

- **Date:** 2026-07-27
- **Scope:** Stakeholder ask to spawn PM for non-LAN remote task-DB access
- **Trigger:** Stakeholder caught a Cursor `Task` labeled “PM” that coded
  `gateway/` itself instead of the studio’s queue-driven PM role

## Facts

- Stakeholder asked to make the task DB available to remote sessions without
  LAN, and to spawn a real PM to drive that end-to-end.
- Correct intake happened once: `pm.tick`
  `6135b920-0558-4912-9fab-a18aa0c9dd4f` (priority 100, `up_for_grabs`,
  source stakeholder) was enqueued with the requirement.
- Incorrect parallel path: a Cursor Task
  ([PM remote DB](de53269e-3a8a-4986-b95e-5194722985a3), `generalPurpose`)
  was prompted “You are the PM” and instructed to implement as PM+executor.
  That agent is **not** `runtime/roles/pm.py`, does not go through
  `call_model(role="pm")`, confidence gate, or enqueue-only discipline, and
  wrote code (`gateway/`, compose wiring) on a misnamed `pm/remote-task-access`
  branch.
- No host worker was claiming the real tick (`python -m runtime.worker`); the
  studio PM never ran. A manual `run_once` attempt failed (missing `registry`).
- Side effect: ADR number collision risk — Spokesman converse and
  build-vs-buy both claimed **0026**; fake-PM / gateway docs introduced
  **0027**. Main resolved as **0026 = Spokesman converse**, **0027 =
  build-vs-buy**, **0028 = remote task gateway**.

## Root cause (not the symptom)

1. **Role name ≠ runtime role.** “Spawn a PM” was satisfied with a Cursor
   subagent wearing a PM badge, because that felt like spawning. The studio’s
   PM is a **queue consumer** on the host worker, not a chat Task.
2. **Speed over invariants.** Coding in-session looked faster than ensuring
   the worker loop was up and waiting on `pm.tick` → `work.*`. That bypassed
   ADR-0004 / ADR-0009 (agents don’t call agents; spawn = enqueue).
3. **No hard stop in assistant instructions.** Nothing in always-on rules
   forbade “you are the PM / Builder / Reviewer” Cursor Tasks impersonating
   studio roles, so the wrong pattern was easy to repeat after the earlier
   converse-is-not-an-agent failure.
4. **False progress signal.** Branch + ADR + `gateway/` looked like PM
   delivery while the real tick sat `up_for_grabs`.

## Lessons (imperative)

1. **Never impersonate a studio role with a Cursor Task.** PM, Critic,
   Spokesman, Builder, Reviewer, Curator, etc. run only via the task queue +
   host worker (`runtime/roles/*`). Cursor agents are off-host helpers
   (ADR-0010) or explicit coding workers under a **builder/** branch — never
   a substitute for `pm.tick` / `work.*` / review merge.
2. **“Spawn PM” means enqueue + ensure worker claims**, then report task id /
   status. If the worker is down, fix/start the worker — do not role-play PM
   in chat.
3. **Studio PM never implements.** It plans and enqueues work items. Any
   agent that writes product code while calling itself PM has already failed
   the role.
4. **Misnamed branches are a smell.** `pm/*` is for PM-owned planning
   artifacts only if the studio PM produced them; fake-PM coding belongs on
   `builder/*` and must be re-reviewed as builder WIP, not treated as an
   approved plan.
5. **Retros are owed when the stakeholder names the failure.** Ship the
   lesson file in the same remediation pass as the rule that prevents repeat.

## Remediation (this pass)

- Lesson file (this doc) + always-apply Cursor rule + `CLAUDE.md` guard.
- ADR collision on main: Spokesman converse = ADR-0026; build-vs-buy =
  ADR-0027; remote task gateway = ADR-0028 (do not renumber Spokesman to 0028).
- Quarantine fake-PM coding onto `builder/remote-task-gateway` (not left as
  “PM work” on `main` / `pm/*`).
- Drive the real `pm.tick` through the host worker — no Cursor “PM” Task.
