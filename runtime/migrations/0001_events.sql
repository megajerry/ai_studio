-- 0001 — events: the append-only event log (ADR-0004, ADR-0012).
--
-- Every agent action, LLM call, and task transition emits a row here. The log is
-- the replayable source of truth: the data-access API only ever INSERTs; there is
-- no UPDATE/DELETE path. trace_id/span_id carry OpenTelemetry context so a run can
-- be reconstructed as a trace (ADR-0012).

CREATE TABLE IF NOT EXISTS events (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ts         timestamptz NOT NULL DEFAULT now(),
    task_id    uuid,                       -- null for task-independent events
    workstream text NOT NULL,
    type       text NOT NULL,              -- e.g. task.created, task.claimed, task.finished
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id   text,                       -- OTel trace context (nullable)
    span_id    text
);

-- Replay a single task's timeline in order.
CREATE INDEX IF NOT EXISTS events_task_ts_idx ON events (task_id, ts);
-- Scan a workstream's timeline in order.
CREATE INDEX IF NOT EXISTS events_workstream_ts_idx ON events (workstream, ts);
