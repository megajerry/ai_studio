---
name: define-success-criteria
description: >-
  Before executing, restate the goal and fix ONE concrete, independently
  checkable success criterion (plus a verifiable marker). The PM confidence-gate
  skill — the run is not "done" until an independent check confirms the criterion.
triggers: [pm, plan, planning, success criteria, confidence gate, define done, acceptance]
when_to_use: >-
  When a PM is planning a task and must fix what "done" means before any work
  starts (architecture §3 confidence gate).
reviewed: true
source: in-repo
---

# Define success criteria (PM confidence gate)

You own *completion*. Before enqueuing any work, pass the confidence gate:

1. **Restate the goal** in one sentence, in your own words.
2. **Define exactly ONE success criterion** that is *independently checkable* —
   a separate Verifier role (read-only) must be able to confirm it without
   trusting your narration. Prefer a criterion that reduces to a deterministic
   check (a file exists and contains a marker; a test passes; a value matches).
3. **Emit a marker** the executor can embed and the verifier can grep for, so
   the pass/fail decision never depends on free-text model output.
4. **Self-score confidence.** If you cannot state a checkable criterion, do NOT
   execute — clarify with the stakeholder or route to research instead.
5. Keep it to ONE criterion. Many vague goals hide multiple; split them into
   separate tasks rather than one fuzzy "done".

Output: the restated goal, the single criterion, and the marker string.
