-- 0002 — tasks: the coordination queue (ADR-0004, ADR-0009, ADR-0010, ADR-0012).
--
-- "Spawning an agent" = enqueueing a task (ADR-0009); agents coordinate ONLY
-- through this queue + the event log, never by direct calls. Each task stores
-- enough state (payload/result) for a fresh agent to resume it, and writes a
-- heartbeat while in progress so the non-agent supervisor can detect a dropped
-- task and re-kick it (ADR-0004). `assignee` targets a task at the host or the
-- off-host worker (ADR-0010); budget_tokens/spent_tokens back per-task cost caps
-- and telemetry (ADR-0012).

CREATE TABLE IF NOT EXISTS tasks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workstream    text NOT NULL,
    type          text NOT NULL,
    status        text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'in_progress', 'blocked', 'done', 'failed')),
    priority      int NOT NULL DEFAULT 0,     -- higher = more urgent
    assignee      text CHECK (assignee IN ('host', 'offhost')),  -- null = any worker
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    result        jsonb,
    heartbeat_at  timestamptz,                -- last liveness ping while in_progress
    claimed_by    text,                       -- worker_id that holds the claim
    budget_tokens bigint,                     -- optional per-task cost cap
    spent_tokens  bigint NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Claim path: pick the highest-priority queued task (FOR UPDATE SKIP LOCKED).
CREATE INDEX IF NOT EXISTS tasks_status_priority_idx
    ON tasks (status, priority DESC, created_at);
-- Supervisor path: find in-progress tasks with a stale heartbeat.
CREATE INDEX IF NOT EXISTS tasks_heartbeat_idx
    ON tasks (heartbeat_at)
    WHERE status = 'in_progress';
