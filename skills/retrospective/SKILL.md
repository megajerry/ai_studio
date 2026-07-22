---
name: retrospective
description: >-
  Distill a completed (or failed) task into durable, reusable lessons for the
  Knowledge-layer corpus — so the studio learns from mistakes instead of
  repeating them. The Retro role skill.
triggers: [retro, retrospective, lesson, lessons, post-mortem, postmortem, learn, review outcome]
when_to_use: >-
  After a task finishes or fails, when the Retro role should capture what to
  keep doing / stop doing as a lesson (architecture §3, §7 Knowledge layer).
reviewed: true
source: in-repo
---

# Retrospective (distill durable lessons)

Turn one task's outcome into lessons future work will have auto-injected:

1. **Establish the facts.** Read the task's event trail — what was planned, what
   the executor did, the verify verdict, any re-kicks/retries. Cite IDs and
   exact values; never speculate past the log.
2. **Name the root cause**, not the symptom. If verify failed, why did the
   executor believe it had succeeded? If it stalled, what was missing?
3. **Write at most 3 lessons**, each one imperative and generalizable beyond
   this task (e.g. "Always embed the success marker in the artifact, not just
   the log"). A lesson that only restates this task is noise.
4. **Scope each lesson** to the narrowest layer that still generalizes
   (this workstream vs. the global corpus) and store it via the Knowledge-layer
   lessons corpus so prompt-assembly can inject it later.
5. **Keep it lossless on facts, terse in prose** (ADR-0013): keep IDs, values,
   and error messages; drop the retelling.

Output: a short list of scoped lessons, each with its root cause.
