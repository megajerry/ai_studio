-- 0010 — budgets: per-workstream spend caps that actually gate (ADR-0006/0012).
--
-- The studio is budget-bounded (docs/cost-model.md §8): a workstream runs under a
-- ceiling, and *raising* that ceiling is a 🛑 stakeholder decision (ADR-0006),
-- never a silent overspend. This table holds, per (workstream, period), a USD
-- and/or token cap. Accrued spend is NOT stored here — it is read live from the
-- append-only `model.call` events (see runtime/budget.py `spent`), so the log
-- stays the single source of cost truth (ADR-0012); this table holds only caps.
--
-- `period` is the window a cap applies to: daily / monthly / rolling_30d /
-- all_time (the workstream's whole history). One row per (workstream, period), so
-- a workstream can carry e.g. a daily AND a monthly cap simultaneously.
--
-- Forward-only and idempotent (like 0007/0008): CREATE ... IF NOT EXISTS is a
-- no-op on re-run and the migration runner skips already-applied files anyway.
-- No data migration: absence of a row = unconstrained, so existing workstreams
-- are unaffected until a cap is explicitly set.

CREATE TABLE IF NOT EXISTS budgets (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workstream  text NOT NULL,
    period      text NOT NULL DEFAULT 'monthly'
                  CHECK (period IN ('daily', 'monthly', 'rolling_30d', 'all_time')),
    cap_usd     numeric,          -- USD ceiling for the window (NULL = no USD cap)
    cap_tokens  bigint,           -- token ceiling for the window (NULL = no token cap)
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    -- One cap row per workstream+period; the upsert in runtime/budget.py keys on it.
    UNIQUE (workstream, period)
);

-- Enforcement path: look up all caps for a workstream on every model call.
CREATE INDEX IF NOT EXISTS budgets_workstream_idx ON budgets (workstream);
