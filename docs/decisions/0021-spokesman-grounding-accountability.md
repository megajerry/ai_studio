# 0021 — Spokesman grounding + accountability (evidence-backed human comms)

- **Status:** Accepted
- **Date:** 2026-07-23

## Context

The Spokesman is the studio's single human-facing interface (architecture §9,
[ADR-0006](0006-stakeholder-comms.md)): it relays 🛑 approvals, 📣 informs, and
🚨 alarms, and answers `status`. Today the notification path
(`spokesman/runtime_bridge.py`) is disciplined — it composes messages from
leak-free event fields only — but the raw relay hole remains: `spokesman/app.py`
`/notify` accepts an arbitrary `{kind, text}` and sends it to the human with **no
provenance and no verification**. Any agent (or a hallucinating one) can put
words in the studio's mouth.

That is the single most dangerous failure mode we have. [ADR-0014](0014-validation-rigor.md)
already says *validators* trust evidence, not claims; this ADR extends the same
doctrine to the **thing we say to the human**, and adds the accountability teeth.

The stakeholder directive is explicit: **everything communicated to the human
MUST be grounded in verifiable evidence**, the Spokesman is **ultimately
accountable** for that, and **fabrication is the worst thing an agent can do** and
carries a zero-tolerance penalty.

## Decision

### 1. The grounding contract

Every human-facing message is decomposed into **claims**. A claim is either:

- a **factual claim** — an assertion about studio state ("task X merged", "spend
  is $Y", "the deploy succeeded"). A factual claim is **not sendable as fact
  unless it carries evidence references** the Spokesman can independently check.
  Unbacked factual prose is not sendable as fact.
- a **judgment** — an opinion / recommendation / plan ("I think we should ship
  Tuesday"). Judgments are allowed but must be **labelled as judgment**, never
  dressed up as verified fact.

Evidence is a typed reference (`EvidenceRef`), never inlined prose: an event
`seq`/id, a task id, a `table+pk` DB row, an artifact hash, a `file:line`, or a
metric query — the same evidence hierarchy as ADR-0014 (run/observe > read code >
inspect logs/metrics/rows/artifacts). A non-judgment `Claim` with **empty
evidence is invalid** at the type level.

### 2. The Spokesman as the accountable verify-or-request-proof gate

The Spokesman is **ultimately accountable** for the truth of everything relayed.
For each factual claim it either:

- **verifies the supplied evidence itself** (resolves each `EvidenceRef` against
  the live event log / task queue / DB / artifact store and confirms it supports
  the statement), or
- **requires the originating agent to supply proof** it then verifies, or
- **finds the proof itself** from the runtime.

A claim is only sendable as fact once its verification status is `verified`. A
claim whose evidence cannot be resolved is `unverifiable`; a claim contradicted by
the evidence is `rejected`. This gate is the S2 wiring on top of the S1
foundation — see "Scope" below.

### 3. Provenance + auditability (invariant 6)

Every claim, its evidence refs, its verification verdict, and every trust action
is recorded so the whole human-facing surface is **observable and replayable**:

- Claim bodies (the human-facing `statement` text) live in the **local DB only**
  (`comms_claims`), never on the append-only event log — mirroring the body-free
  discipline of ADR-0020 (trajectories) and invariant 5.
- The event log carries **body-free provenance events** — `comms.claim_verified`,
  `comms.claim_rejected`, `comms.fabrication_detected`, `trust.strike`,
  `trust.capability_revoked` — with only ids / identity / status / kind / counts.
  A replay reconstructs *who claimed what got verified/rejected and what penalty
  followed*, without ever leaking the claim text onto the wire.

### 4. The trust ledger

An `identity_trust` table tracks, per **role / agent-workflow-identity** string, a
`trust_state` (`trusted` | `quarantined` | `revoked`), a `human_relay_allowed`
flag, a `strikes` counter, and `last_strike_at`. All writes go through a single
guarded writer (`runtime/trust.py`), mirroring the `runtime/tasks.py` /
`runtime/trajectory.py` discipline (one guard, injectable `now`, body-free
events, no ad-hoc UPDATEs). The ledger is the durable, auditable source of truth
for "who is allowed to speak to the human".

### 5. Zero-tolerance fabrication penalty

Fabrication / hallucination / knowingly-false info relayed as fact is the
**worst** offense. A single fabrication strike:

1. **blocks** the offending claim (never reaches the human),
2. **permanently revokes** the offending identity's human-facing-relay capability
   (`human_relay_allowed = false`, `trust_state = revoked`),
3. **quarantines** it from claiming tasks (its `trust_state` gates task claiming
   in a later track),
4. records a **permanent strike** (`strikes += 1`, `last_strike_at`),
5. **escalates 🚨** (a `comms.fabrication_detected` provenance event the Spokesman
   surfaces as an alarm per ADR-0006), and
6. **cascades to the verifier chain** — any verifier/identity that passed the
   false claim as verified is itself accountable and takes a strike (the same
   guard applied to each link that signed off), so a rubber-stamp is not a way to
   launder a fabrication.

This is deliberately harsher than ADR-0014's per-claim UNVERIFIED verdict: an
honest "I couldn't verify this" is `unverifiable` (no strike); asserting a
falsehood as verified fact is a fabrication (permanent revocation). The
distinction is the whole point.

**Follow-up (ill-formed `expected` is not fabrication).** The gate must
distinguish "you lied" from "you wrote the evidence spec wrong". A contradiction
(→ fabrication) is only declared against an `expected` the resolver can actually
*interpret* as an assertion for its `EvidenceKind`: a **task** status must be a
bare value from the canonical lifecycle set; a **db_row** `expected` must be
`col=val` on a column that exists; a **metric** `expected` must be a numeric count
(all whitelisted metrics are counts); a **file** `expected` must be a 64-char
sha256 digest. If `expected` cannot be parsed as a valid assertion for the kind
(wrong syntax — e.g. a `db_row`-style `status=merged` on a `task` — an unknown
column, a non-status, a non-numeric metric, a non-hash file), the ref is
**malformed** → the claim is `unverifiable` (withheld + `comms.proof_requested`,
**no strike, no revocation**), never `rejected`. An honest agent that merely
mis-formats its spec must not be branded a permanent fabricator; only a
well-formed `expected` whose value genuinely contradicts the source of truth
still triggers the zero-tolerance penalty. Implemented in
`spokesman/grounding_gate.py` (`RefResolution.malformed`, per-kind `expected`
validation) with the same fail-closed guarantees.

## Scope — what lands in S1 (this) vs later

- **S1 (this track):** the foundation, additive and non-breaking — this ADR, the
  `identity_trust` + `comms_claims` schema (migration 0012), the typed grounding
  contract (`runtime/grounding.py`: `EvidenceRef` + `Claim` + validation), the
  guarded trust ledger (`runtime/trust.py`: record/verify claims, zero-tolerance
  strikes, relay gate), the body-free `comms.*` / `trust.*` event constants, and a
  `COMMS_HUMAN_RELAY` policy capability + a thin `is_relay_allowed`-backed check
  helper. **No change to the `/notify` contract or the Spokesman send path.**
- **S2 (next track):** wire the Spokesman send path to the gate — decompose an
  outbound message into claims, verify/request-proof, block unbacked facts, and
  gate `/notify` on `COMMS_HUMAN_RELAY` + `is_relay_allowed`. This owns
  `spokesman/app.py`.
- **S3:** the verification engine that resolves each `EvidenceRef` kind against
  the runtime, the cascade automation across the verifier chain, and quarantine
  enforcement in the task-claim path.

## Consequences

- Human comms become auditable and replayable end-to-end without leaking claim
  bodies onto the event log (invariant 6, ADR-0020 body-free pattern).
- The runtime gains a durable trust ledger and a hard, single-strike deterrent
  against the highest-severity failure mode, complementing ADR-0014's
  evidence-over-claims doctrine for validators.
- A revoked identity can never again speak to the human — recovery is a
  deliberate, human-driven act, not an automatic decay (kept out of S1 on
  purpose).
- Cost: producing evidence for every factual claim and verifying it costs tokens
  and latency — an accepted price for never putting an unverified word in the
  studio's mouth, scaled by the adaptive-intensity model (ADR-0003).

## References

- Invariants 1 (agents coordinate via the queue/log, not direct calls) and 6
  (everything emits events + is replayable) — `CLAUDE.md`.
- [ADR-0006](0006-stakeholder-comms.md) — stakeholder comms tiers (🛑/📣/🚨).
- [ADR-0014](0014-validation-rigor.md) — validators trust evidence, not claims.
- [ADR-0015](0015-task-lifecycle-state-machine.md) — canonical lifecycle /
  single-guard discipline (mirrored by the trust ledger).
- [ADR-0020](0020-trajectory-observability.md) — body-free events + guarded-writer
  pattern this reuses.
- The policy engine (`runtime/policy.py`, `runtime/capabilities.py`) — the relay
  capability + gate hook.
