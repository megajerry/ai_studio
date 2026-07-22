-- 0008 — canonical task lifecycle state machine + dependencies + telemetry
-- (ADR-0015, extends ADR-0012). Forward-only and idempotent: safe to re-run.
--
-- Widens tasks.status to the canonical set, migrates legacy rows, adds the
-- dependency graph (depends_on) + per-agent lifecycle columns, and creates the
-- append-only task_transitions telemetry table.

-- 1. Migrate legacy status values BEFORE re-tightening the CHECK. Idempotent:
--    a second run finds no legacy rows and updates nothing. The CHECK is dropped
--    first so the UPDATEs cannot violate the old-or-new constraint mid-migration.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;

UPDATE tasks SET status = 'up_for_grabs' WHERE status = 'queued';
UPDATE tasks SET status = 'merged'       WHERE status = 'done';
UPDATE tasks SET status = 'abandoned'    WHERE status = 'failed';
-- in_progress / blocked are unchanged (same names in the canonical set).

-- 2. Re-add the CHECK over the canonical set. Same constraint name as the inline
--    one Postgres auto-named in 0002, so the DROP IF EXISTS above removes either
--    version and this re-adds cleanly on every run.
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check CHECK (
    status IN (
        'up_for_grabs', 'claimed', 'in_progress', 'blocked',
        'ready_for_review', 'reviewer_blocked', 'approved', 'merged', 'abandoned'
    )
);

-- New default for freshly-created tasks (PM enqueues work as up_for_grabs).
ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'up_for_grabs';

-- 3. Per-agent lifecycle + dependency columns (all idempotent).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS agent_type text;      -- claiming agent kind
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS claimed_at timestamptz; -- when grabbed
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS depends_on uuid[] NOT NULL DEFAULT '{}';

-- Grab path: an up_for_grabs task ordered by priority; the grabbability check
-- (all prereqs merged) is a NOT EXISTS over unnest(depends_on).
CREATE INDEX IF NOT EXISTS tasks_status_priority_idx
    ON tasks (status, priority DESC, created_at);
-- Supervisor path: actively-held tasks (claimed/in_progress) with a stale heartbeat.
CREATE INDEX IF NOT EXISTS tasks_heartbeat_idx
    ON tasks (heartbeat_at)
    WHERE status IN ('claimed', 'in_progress');

-- 4. Append-only lifecycle telemetry: one row per guarded transition, with the
--    acting agent + latency since the previous transition (ADR-0012/0015).
CREATE TABLE IF NOT EXISTS task_transitions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id     uuid NOT NULL REFERENCES tasks(id),
    from_status text,                       -- null for the very first transition
    to_status   text NOT NULL,
    agent_id    text,                       -- worker/agent id that made the move
    agent_type  text,                       -- kind of agent (executor/pm/reviewer/…)
    at          timestamptz NOT NULL DEFAULT now(),
    latency_ms  bigint                      -- ms since this task's previous transition
);

CREATE INDEX IF NOT EXISTS task_transitions_task_idx
    ON task_transitions (task_id, at);
CREATE INDEX IF NOT EXISTS task_transitions_agent_idx
    ON task_transitions (agent_type, to_status);
