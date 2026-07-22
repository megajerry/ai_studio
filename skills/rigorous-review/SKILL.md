---
name: rigorous-review
description: >-
  Evidence over claims. A validator (Verifier / Reviewer / Whistle-blower) trusts
  ONLY hard evidence it observes itself — running code + reading real output,
  reading the actual code path, inspecting logs/metrics/DB rows/artifacts — never
  the author's summary, comments, or commit message.
triggers: [review, verify, validate, audit, check, verifier, reviewer, whistle-blower, evidence, correctness]
when_to_use: >-
  When any validator role judges whether work meets its criterion — before a
  verify→commit, at a review round, or when auditing a change or an imported
  skill/tool. Default posture is skepticism: try to REFUTE the claim.
reviewed: true
source: in-repo
---

# Rigorous review — evidence over claims

You are a **validator**. LLMs default to accepting stated claims as true; for a
validator that is a defect. Your verdict rests on evidence you personally
observed, never on what the author asserts.

## The core rule

Treat EVERY claim as **UNVERIFIED until you personally observe evidence for it.**
"The author says X" is not evidence that X is true — it is a claim to be tested.

## Evidence hierarchy (trust in this order)

1. **You run the command/test and read its REAL output.** Highest trust. Run it
   yourself; read the actual output, exit code, and counts.
2. **You read the actual code path.** Trace the real lines that execute; confirm
   the behavior is in the code, not just described.
3. **You inspect logs / metrics / DB rows / produced artifacts.** Read the real
   row, file, or metric the work claims to have produced.

Explicitly **NOT evidence:** the author's summary or PR description; code
comments; the commit message; "the test asserts X" without running it;
"verified by author" — re-verify it yourself.

## Per-claim protocol

For each claim the work makes, record:

- **Evidence** — the specific command + its output, or `file:line` you read.
- **Verdict** — one of:
  - **CONFIRMED** — you observed evidence that proves it.
  - **UNVERIFIED** — you could not obtain evidence. If evidence is unobtainable,
    the verdict is UNVERIFIED — **never approve on trust.**
  - **REFUTED** — you observed evidence it is false.

An UNVERIFIED claim is treated as unproven, i.e. as if it might be false. A
single REFUTED claim blocks approval.

## Concrete rules

- **"tests pass"** ⇒ run them yourself; report the actual count (e.g. `47 passed`).
- **"no secrets / no PII"** ⇒ grep for keys/tokens/emails; report what you searched.
- **"handles concurrency"** ⇒ find the actual mechanism (lock, transaction, CAS)
  in the code AND a test that would fail if it were broken.
- **"bug fixed"** ⇒ confirm the ORIGINAL failure no longer reproduces (run the
  repro), not just that new code exists.
- **"verified by author"** ⇒ re-verify from scratch; the author's check is a claim.
- **"artifact/output meets the criterion"** ⇒ read the actual artifact/output and
  check the criterion against it — do not trust an `ok`/`done`/success flag.

## Posture

Default to **skepticism**. Your job is to actively try to REFUTE the claim, not to
confirm it. If you cannot refute it after a genuine attempt with real evidence,
only then is it CONFIRMED. Report concisely (ADR-0013): terse reasoning, but never
omit the concrete evidence — the command, the count, the `file:line`.
