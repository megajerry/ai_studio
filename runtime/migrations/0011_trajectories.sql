-- 0011 — trajectory observability: the reasoning TRAJECTORY as first-class data
-- (ADR-0020, extends ADR-0012 telemetry / ADR-0013 context / ADR-0015 lifecycle).
--
-- The repo already persists ACTIONS + STATE (task_transitions, events, model.call
-- telemetry) but NOT the ordered causal CHAIN of how an agent (especially the PM)
-- reached a decision: what it observed, options weighed, what it decided + why,
-- where the Critic pushed back, what it revised, when it escalated. This adds two
-- tables for that chain, plus a link from a decomposition trajectory to the tasks
-- it created (outcome attribution).
--
-- Retention model (ADR-0020): capture FULL VERBATIM traces on the write path
-- (fast, no inline scrubbing); bound footprint with a TTL (`expires_at`) and a
-- later verbatim→lean rotation (a learning agent distills the verbatim `rationale`
-- bodies losslessly-on-outcome). Bodies (goal/summary/rationale) live in the LOCAL
-- DB ONLY — they are NEVER written to the append-only event log (which stays
-- body-free, carrying only ids/types/seq/step_type — same discipline as 0009).
--
-- All writes go through the single guarded writer runtime/trajectory.py (mirroring
-- runtime/tasks.py::transition + runtime/events.py — no ad-hoc INSERT/UPDATE).
--
-- Forward-only and idempotent (like 0006/0008/0009): CREATE ... IF NOT EXISTS is a
-- no-op on re-run and the migration runner skips already-applied files anyway.

-- 1. One trajectory = one bounded reasoning episode (a role working toward a goal).
CREATE TABLE IF NOT EXISTS trajectories (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role               text NOT NULL,           -- pm / critic / executor / retro / …
    workstream         text NOT NULL,
    goal               text NOT NULL,           -- body: LOCAL DB ONLY, never on the wire
    status             text NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','closed')),
    -- verbatim = full rationale bodies; lean = distilled (rotated by the learning
    -- agent via compact_to_lean, lossless on outcome-relevant facts).
    retention_tier     text NOT NULL DEFAULT 'verbatim'
                         CHECK (retention_tier IN ('verbatim','lean')),
    started_at         timestamptz NOT NULL DEFAULT now(),
    ended_at           timestamptz,             -- set on close
    expires_at         timestamptz,             -- TTL horizon; NULL = never expires
    context_size_start int,                     -- context tokens when the episode began (ADR-0013)
    context_size_peak  int,                     -- peak context tokens over the episode
    tokens             bigint,                  -- rolled-up token spend (nullable)
    cost_usd           numeric,                 -- rolled-up $ spend (nullable)
    latency_ms         bigint,                  -- total wall-clock (set on close)
    outcome_summary    text,                    -- body: LOCAL DB ONLY, never on the wire
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- 2. Ordered causal steps within a trajectory. `seq` is a gapless per-trajectory
--    monotonic key (like events.seq, but scoped per trajectory), assigned under a
--    FOR UPDATE lock on the parent row in runtime/trajectory.py::add_step.
CREATE TABLE IF NOT EXISTS trajectory_steps (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trajectory_id      uuid NOT NULL REFERENCES trajectories(id) ON DELETE CASCADE,
    seq                bigint NOT NULL,         -- 1-based, monotonic per trajectory (no gaps)
    step_type          text NOT NULL
                         CHECK (step_type IN (
                             'observe','plan','decide','consult',
                             'revise','decompose','escalate','commit')),
    summary            text NOT NULL,           -- body: LOCAL DB ONLY, never on the wire
    rationale          text,                    -- FULL VERBATIM body; distilled on lean rotation
    options_considered jsonb NOT NULL DEFAULT '[]'::jsonb,
    choice             text,                    -- outcome-relevant: preserved across lean rotation
    confidence         real,                    -- outcome-relevant: preserved across lean rotation
    refs               jsonb NOT NULL DEFAULT '{}'::jsonb,  -- task ids / event ids / critic verdicts
    context_size       int,                     -- context tokens at this step (ADR-0013)
    tokens             bigint,
    cost_usd           numeric,
    latency_ms         bigint,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- 3. Link a decomposition trajectory → the tasks it created (outcome attribution):
--    a PM `decompose` step's trajectory is joinable to the work tasks it spawned,
--    so a decision can be scored against how its tasks actually turned out. SET
--    NULL on delete so expiring a trajectory (TTL) never orphans/blocks a task.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS trajectory_id uuid REFERENCES trajectories(id) ON DELETE SET NULL;

-- 4. Indexes.
-- Replay a trajectory's steps in true causal order; UNIQUE enforces the no-gap /
-- no-duplicate per-trajectory seq contract at the DB level.
CREATE UNIQUE INDEX IF NOT EXISTS trajectory_steps_traj_seq_idx
    ON trajectory_steps (trajectory_id, seq);
-- Primary read path: "the trajectories for role R in workstream W".
CREATE INDEX IF NOT EXISTS trajectories_role_workstream_idx
    ON trajectories (role, workstream);
-- TTL sweeps / expiry scans without a full table scan.
CREATE INDEX IF NOT EXISTS trajectories_expires_at_idx
    ON trajectories (expires_at);
-- Outcome-attribution join from tasks back to their originating trajectory.
CREATE INDEX IF NOT EXISTS tasks_trajectory_idx
    ON tasks (trajectory_id);
