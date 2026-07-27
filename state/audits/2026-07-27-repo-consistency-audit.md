# Daily repo consistency/breakage audit — 2026-07-27

Off-host agent, 7-auditor fan-out across all subsystems + a ground-truth run.
**Codebase is fundamentally sound:** all 80 modules import, `runtime.demo` green + self-cleans
(ADR-0029), migrations contiguous 0001..0018 with zero column drift, all 91 event types wired
producer↔consumer, config/secret coverage complete, docker-compose coherent. `pytest`:
1147 passed, 2 failed (1 env `python-multipart`, 1 the fixture bug below), 0 skips.

## Confirmed bugs — fixes spawned (branch → independent review → merge)
| # | Sev | Bug | Fix branch |
|---|-----|-----|-----------|
| G1 | HIGH (security) | Gateway: `claim`-scoped remote can steal host-pinned work via `{"assignee":"host"}` (proven exploit); + 500 on bad assignee. Contradicts ADR-0028. | `builder/gateway-assignee-scope` |
| M1 | HIGH (governance) | Spokesman conversation spend invisible: `converse._agent_turn` uses `NullEventSink()` with a real conn → cost event dropped, reservation nets to zero. | `builder/spokesman-audit-fixes` |
| A1 | MED/HIGH (liveness) | Stale `claimed` task livelocks forever: terminal recovery rungs only act on `in_progress` (proven). | `builder/recovery-claimed-livelock` |
| S1 | MED | Notify path (`/poll`) skips the noise filter that `status`/dashboard apply → human pinged about ephemeral pytest/demo items that then show as "0 pending". | `builder/spokesman-audit-fixes` |
| S2 | MED/LOW | `migrate(conn)` leaves a non-autocommit connection INTRANS → the one red test; latent footgun. | `builder/migrate-txn-hygiene` |
| S3 | LOW | `yes`/`no`/`reject` reserved as approval verbs hijack conversational replies. | `builder/spokesman-audit-fixes` |
| S4 | LOW | Hex-suffix noise regex false-positives real names (`facade`,`decade`) → drops real workstreams. | `builder/spokesman-audit-fixes` |
| R1 | MED (landmine) | `feature_request` tasks abandoned (no worker dispatch → generic claim → "unknown type"); intended `pm.triage_request` never called live. | `builder/feature-request-triage` |

## Flagged for a human/PM decision — NOT auto-fixed (change behavior/cost)
- **Dead roles / unwired cadences (R2–R4):** the **Critic** is never consulted in the live PM path
  (worker never passes `critic=`); the **Capacity Steward** runs nowhere; **Sourcing / Failure-analyst /
  Skill-lifecycle** have worker handlers but nothing enqueues them on any cadence (scheduler emits only
  `pm.tick`/`replan`). Decision: wire these into the loop (and at what cadence), or leave dormant? Enabling
  them adds model spend + autonomy.
- **Free-text in the event log (C1):** `goal`/`reason` (incl. model-authored verifier rationale) embedded
  in `events.payload`, which the system documents as body-free (invariant #6). Local-only (not a
  cross-boundary leak). Decision: redact to keep the log body-free, or annotate as intentional (useful for replay)?
- **Dead router budget-downshift (M2):** no production caller passes `budget_ctx`; the downshift loop is an
  inert single-step. Decision: wire `budget_ctx` for real graceful downshift, or simplify/remove the misleading loop?
- **Grounding gate not on conversational replies (S5):** by ADR-0026 design (gate is for `/notify` claims). Note only.
- **ADR-0029 follow-up:** tighten `runtime.demo`'s approval snapshot with a role/status filter (shared-live-DB edge; LOW).

Verified explicitly clean: state-machine edges + no ad-hoc status writes, model routing (every task_type
resolves), gateway auth/scope/workstream/ownership gates, body-free `EVENT_HUMAN_MESSAGE`, conversation-memory
schema, all runtime signatures the spokesman/gateway call.
