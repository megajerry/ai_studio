# 0014 — Validators trust evidence, not claims

- **Status:** Accepted
- **Date:** 2026-07-22

## Context

LLMs default to accepting stated claims as true — the author's summary, code
comments, a commit message, or "the test asserts X" get taken at face value. For
most roles that is a harmless convenience. For a **validator** — the Verifier
today, and the future **Reviewer / Whistle-blower** ([ADR-0003](0003-workstream-operating-model.md))
— it is a defect: the whole point of an independent check is to *not* trust the
author's narration. A hallucination cascade (ADR-0003's named failure mode) is
exactly what happens when a validator approves on trust.

Our operating model already commits work only after an *independent* check
(verify → commit). But "independent" is meaningful only if that check rests on
**evidence the validator observed itself**, not on the executor's assertion of
success.

## Decision

All validator agents adopt an **evidence-over-claims** doctrine. It applies to the
**Verifier** (now), the future **Reviewer / Whistle-blower**, and any review /
audit agent.

1. **Every claim is UNVERIFIED until the validator personally observes evidence.**
   The author saying X is a claim to be tested, not evidence that X is true.
2. **Evidence hierarchy** (trust in this order): (1) run the command/test and read
   its real output; (2) read the actual code path; (3) inspect logs / metrics /
   DB rows / produced artifacts. Explicitly **not** evidence: the author's
   summary, comments, the commit message, or "the test asserts X" without running
   it.
3. **Per-claim verdict:** cite specific evidence (command + output, or `file:line`)
   and record CONFIRMED / UNVERIFIED / REFUTED. If evidence is unobtainable the
   verdict is UNVERIFIED — **never approve on trust.** A REFUTED claim blocks.
4. **Concrete rules:** "tests pass" ⇒ run them, report the count; "no secrets" ⇒
   grep; "handles concurrency" ⇒ find the mechanism + a test that would fail if
   broken; "bug fixed" ⇒ confirm the original failure no longer reproduces;
   "verified by author" ⇒ re-verify.
5. **Default to skepticism** — the validator's job is to try to REFUTE the claim.

The doctrine is packaged as a reusable, reviewed **`rigorous-review`** skill
([ADR-0008](0008-adopt-agent-skills-standard.md)) so any validator selects and
injects it on demand, the same way the PM injects `define-success-criteria`. The
Verifier's verdict is made concrete: it re-reads the ACTUAL artifact and checks
the success criterion against its real contents (the deterministic evidence),
never the Executor's `result.ok` claim.

## Consequences

- The Verifier role injects `rigorous-review` when a skill registry is supplied
  (behavior-preserving with none) and its verdict is decided by observed artifact
  contents, not the executor's success flag. A test proves a false "done" claim
  over a non-conforming artifact still FAILS.
- The future Reviewer / Whistle-blower role inherits the doctrine + skill for
  free; building it is deferred, but the contract is fixed here.
- Validation costs more tokens/time (re-running, re-reading) than trusting a
  summary — an accepted price, scaled by the adaptive-intensity model (ADR-0003):
  spend more rigor where the error rate / blast radius is high.
- Reporting stays concise (ADR-0013): terse reasoning, but the concrete evidence
  (command, count, `file:line`) is never omitted.

## References

- [ADR-0003](0003-workstream-operating-model.md) — independent verification;
  hallucination-cascade failure mode.
- [ADR-0008](0008-adopt-agent-skills-standard.md) — Agent Skills; reviewed gate.
- [ADR-0013](0013-context-management.md) — concise but fact-complete reporting.
