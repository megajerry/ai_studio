-- 0009 — experiments: the venture-studio brain's first object (ADR-0016,
-- architecture §11 — the moat = define experiment → evaluate signal → kill/scale).
--
-- An `experiment` is a bounded bet: a hypothesis, a measurable success metric
-- (name/target/comparator), a token/$ budget, and an evidence-based verdict.
-- runtime/experiment/api.py starts work items toward the hypothesis (tagged with
-- experiment_id), then evaluate_experiment() reads the metric + spend from
-- telemetry (task_cost / observation events) and applies the kill/scale rule.
--
-- Status lifecycle (guarded in runtime/experiment/models.py, mirroring the task
-- state machine): proposed → running → evaluated → (kept | scaled | killed).
-- `decision` records the terminal verdict; `success_metric` is stored as JSONB.
--
-- Forward-only and idempotent (like 0007/0008): CREATE ... IF NOT EXISTS is a
-- no-op on re-run and the migration runner skips already-applied files anyway.

CREATE TABLE IF NOT EXISTS experiments (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workstream     text NOT NULL,
    hypothesis     text NOT NULL,
    -- {name, target, comparator, aggregate} — validated in code before insert.
    success_metric jsonb NOT NULL,
    budget_tokens  bigint,                       -- token ceiling (null = uncapped)
    budget_usd     numeric,                      -- $ ceiling   (null = uncapped)
    status         text NOT NULL DEFAULT 'proposed'
                     CHECK (status IN ('proposed','running','evaluated','kept','scaled','killed')),
    decision       text
                     CHECK (decision IS NULL OR decision IN ('kept','scaled','killed')),
    observed_value double precision,             -- the metric value seen at evaluation (evidence)
    spent_tokens   bigint  NOT NULL DEFAULT 0,   -- measured spend the verdict was judged against
    spent_usd      numeric NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,                  -- set on proposed → running
    evaluated_at   timestamptz                   -- set on running → evaluated
);

-- Primary read path: "the experiments in workstream W with status S" (a
-- workstream's live/terminal bets), per the acceptance criteria.
CREATE INDEX IF NOT EXISTS experiments_workstream_status_idx
    ON experiments (workstream, status);
