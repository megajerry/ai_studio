-- 0017 — Spokesman conversational prep cache + handoffs (ADR-0026).
--
-- Prep cache: anticipatory studio context for low-latency answers. Cache is a
-- latency aid only — grounding (ADR-0021) still verifies against the live DB.
--
-- Handoffs: rare specialist↔human sessions; activated only after a human
-- approves a handoff.propose approval. Messages still relay through Spokesman.

CREATE TABLE IF NOT EXISTS spokesman_prep_cache (
    cache_key   text PRIMARY KEY,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS spokesman_handoffs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_id     uuid REFERENCES approvals (id),
    role            text NOT NULL,                      -- e.g. pm, critic
    status          text NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed', 'active', 'ended')),
    workstream      text NOT NULL DEFAULT 'productivity',
    reason          text NOT NULL DEFAULT '',           -- body; not on the wire
    created_at      timestamptz NOT NULL DEFAULT now(),
    activated_at    timestamptz,
    ended_at        timestamptz,
    expires_at      timestamptz
);

CREATE INDEX IF NOT EXISTS spokesman_handoffs_status_idx
    ON spokesman_handoffs (status);
CREATE INDEX IF NOT EXISTS spokesman_handoffs_approval_idx
    ON spokesman_handoffs (approval_id);
