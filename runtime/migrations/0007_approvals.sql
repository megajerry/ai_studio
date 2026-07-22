-- 0007 — approvals: the human-in-the-loop grant store for 🔴 actions
-- (architecture §5, ADR-0006, CLAUDE.md approval tier 🔴).
--
-- A 🔴 (NEEDS_APPROVAL) tool call cannot execute until a human grants it. The
-- policy engine (runtime/policy.py) decides NEEDS_APPROVAL; runtime/enforce.py
-- turns that into a *pending* row here and refuses to run the tool. A human
-- resolves it (approve/deny); an approved row is a one-shot GRANT that authorizes
-- exactly ONE execution (then it is marked `consumed`). See runtime/approvals.md.
--
-- `request_fingerprint` is a stable hash of (task_id + tool + sorted capabilities)
-- so a later retry of the *same* action can be matched to its grant. It is NOT a
-- secret and carries no argument values (CLAUDE.md invariant 5).
--
-- Forward-only and idempotent (like 0004/0005): CREATE ... IF NOT EXISTS is a
-- no-op on re-run and the migration runner skips already-applied files anyway.

CREATE TABLE IF NOT EXISTS approvals (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id             uuid,                    -- the blocked task, if any
    role                text NOT NULL,
    tool                text NOT NULL,
    capabilities        text[] NOT NULL DEFAULT '{}',  -- capability names (no arg values)
    tier                text NOT NULL,
    reason              text NOT NULL DEFAULT '',
    request_fingerprint text NOT NULL,           -- stable hash: matches a grant to a pending action
    status              text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'approved', 'denied', 'consumed')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at         timestamptz,             -- set when approved/denied
    resolver            text                     -- who resolved it (human/spokesman id)
);

-- Digest/queue path: list all currently-pending approvals for the Spokesman.
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals (status);
-- Grant-matching path: find an approved (un-consumed) grant for a fingerprint.
CREATE INDEX IF NOT EXISTS approvals_fingerprint_idx ON approvals (request_fingerprint);
