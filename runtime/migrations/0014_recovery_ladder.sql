-- 0014 — graduated recovery ladder: progress-aware supervisor recovery
-- (ADR-0023, extends ADR-0004/0015/0020).
--
-- Today the non-agent supervisor has only TWO moves for a stale in-progress task:
-- re-kick (reset to up_for_grabs, discard all in-flight progress, bump `retries`,
-- re-run from scratch) or, after SUPERVISOR_MAX_RETRIES, force-abandon. That is a
-- binary cliff: a transient API hiccup costs a full progress-discarding reset, and
-- a large task can burn all its retries making ZERO net progress and then just die.
--
-- ADR-0023 makes recovery a graduated, progress-aware LADDER (cheapest first):
--   nudge+grace → re-kick → (no-progress) escalate-to-PM (task.stuck) → abandon.
-- This migration adds the bookkeeping the ladder needs. It is ADDITIVE +
-- BACK-COMPATIBLE: every column is nullable or defaulted so existing rows and the
-- old two-move behavior are unaffected until the new supervisor writes them.
--
--   * last_progress_at    — watermark of the last observed NET progress for the
--                           current attempt (set when work begins / advanced at each
--                           re-kick). The progress detector counts model.call events
--                           + trajectory_steps NEWER than this watermark; NULL = no
--                           baseline yet (treated as "any signal counts").
--   * no_progress_rekicks — consecutive re-kicks that observed NO net progress. Reset
--                           to 0 the moment progress is seen. When it crosses
--                           SUPERVISOR_STUCK_THRESHOLD (default 2, < max_retries) the
--                           supervisor STOPS re-kicking and escalates (task.stuck).
--   * stall_reason        — last recorded stall/escalation reason CODE (e.g.
--                           'no_progress'); a short attributable label, never body text.
--   * nudged_at           — when a nudge was issued for the CURRENT stall episode.
--                           Set on first stall detection (defers the re-kick for a
--                           grace window so a transient stall recovers with progress
--                           preserved); cleared when the worker heartbeats again or on
--                           the eventual re-kick. NULL = no open nudge episode.
--
-- Forward-only and idempotent (like 0003/0010/0013): ADD COLUMN IF NOT EXISTS is a
-- no-op on re-run and the migration runner also skips already-applied files.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_progress_at    timestamptz;          -- NULL = no baseline yet
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS no_progress_rekicks int  NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS stall_reason        text;                 -- reason CODE, never body text
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS nudged_at           timestamptz;          -- NULL = no open nudge episode
