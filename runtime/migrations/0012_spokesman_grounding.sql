-- 0012 — Spokesman grounding + accountability foundation (ADR-0021).
--
-- Everything relayed to the human MUST be grounded in verifiable evidence, the
-- Spokesman is ULTIMATELY accountable for that, and fabrication is the worst thing
-- an agent can do (zero-tolerance penalty). This migration lands the S1 FOUNDATION
-- only — the durable schema — and is ADDITIVE + non-breaking: it does NOT touch the
-- /notify contract or the Spokesman send path (S2 owns that).
--
-- Two tables:
--   1. identity_trust  — the durable trust ledger keyed by role / agent-workflow-
--      identity string; the source of truth for "who may speak to the human".
--   2. comms_claims    — one row per human-facing claim, its evidence refs, and its
--      verification verdict. The `statement` body is LOCAL DB ONLY — it is NEVER
--      written to the append-only event log (which stays body-free, carrying only
--      ids/identity/status/kind/counts — the ADR-0020 discipline, invariants 5 & 6).
--
-- All writes go through the single guarded writer runtime/trust.py (mirroring
-- runtime/tasks.py::transition + runtime/trajectory.py — no ad-hoc INSERT/UPDATE).
--
-- Forward-only and idempotent (like 0009/0011): CREATE ... IF NOT EXISTS is a no-op
-- on re-run and the migration runner skips already-applied files anyway.

-- 1. Trust ledger. One row per role / agent-workflow-identity string.
CREATE TABLE IF NOT EXISTS identity_trust (
    identity            text PRIMARY KEY,        -- a role / agent-workflow-identity string
    trust_state         text NOT NULL DEFAULT 'trusted'
                          CHECK (trust_state IN ('trusted','quarantined','revoked')),
    human_relay_allowed boolean NOT NULL DEFAULT true,  -- may speak to the human?
    strikes             int NOT NULL DEFAULT 0,         -- permanent, never decremented
    last_strike_at      timestamptz,             -- when the most recent strike landed
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- 2. Human-facing claims + their evidence + verification verdict.
CREATE TABLE IF NOT EXISTS comms_claims (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_ref         text,                    -- groups the claims of one outbound message (nullable)
    originating_identity text NOT NULL,          -- who made the claim
    statement           text NOT NULL,           -- the human-facing claim: LOCAL DB ONLY, never on the wire
    evidence            jsonb NOT NULL DEFAULT '[]'::jsonb,  -- list of EvidenceRef dicts
    is_judgment         boolean NOT NULL DEFAULT false,      -- judgment (labelled) vs factual claim
    verification_status text                     -- NULL until checked
                          CHECK (verification_status IN ('verified','rejected','unverifiable')),
    verified_by         text,                    -- the identity that ran the verification
    reason              text,                    -- why verified/rejected/unverifiable (local)
    seq                 bigint NOT NULL,         -- monotonic append order (like events.seq)
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Monotonic claim ordering: a gapless global sequence assigned under the guarded
-- writer, so claims replay in true creation order (mirrors events.seq).
CREATE SEQUENCE IF NOT EXISTS comms_claims_seq;

-- 3. Indexes.
-- "all claims by identity X" — the accountability / audit read path.
CREATE INDEX IF NOT EXISTS comms_claims_identity_idx
    ON comms_claims (originating_identity);
-- "all claims in verification status S" — the verify-gate work queue.
CREATE INDEX IF NOT EXISTS comms_claims_status_idx
    ON comms_claims (verification_status);
-- Trust-state scans (e.g. list all revoked/quarantined identities).
CREATE INDEX IF NOT EXISTS identity_trust_state_idx
    ON identity_trust (trust_state);
