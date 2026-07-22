-- 0003 — task retries: the supervisor's re-kick counter (ADR-0004).
--
-- The non-agent supervisor re-kicks any in-progress task whose heartbeat has
-- gone stale (reset to 'queued', clear the claim, emit `task.rekicked`). This
-- column bounds that loop: once `retries` reaches the configured max the
-- supervisor force-fails the task (emitting `task.failed_exhausted`) instead of
-- re-kicking forever, so a genuinely-stuck task cannot churn indefinitely.
--
-- Forward-only and idempotent: ADD COLUMN IF NOT EXISTS is a no-op if the column
-- already exists (and the migration runner skips already-applied files anyway).

ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS retries int NOT NULL DEFAULT 0;
