-- 0013 — graduated capacity governance: tiered thresholds + two-level ceiling
-- (ADR-0022, extends ADR-0006/0012).
--
-- Today `budgets` is a HARD BINARY cap: a call under `cap_usd`/`cap_tokens`
-- proceeds, a call that would cross it is blocked and raises a 🛑 "raise budget"
-- approval. That leaves no room to react — a workstream that hits the wall can't
-- even afford to wind down or escalate. ADR-0022 makes capacity graduated:
-- warn → throttle → reserve → hard-stop, where a RESERVE BUFFER near the cap is
-- spendable ONLY on wind-down/escalation, so a workstream can pivot/escalate
-- BEFORE breaching. Enforcement stays deterministic in runtime/budget.py.
--
-- This migration is ADDITIVE + BACK-COMPATIBLE:
--   * Three nullable threshold fractions on `budgets`. They are NULLABLE WITH NO
--     DEFAULT ON PURPOSE: an existing row (and any new row that leaves them NULL)
--     keeps the OLD hard-cap-only behavior — it has only an `ok` zone up to the
--     cap and an `over` zone past it, exactly as before. Graduated zones
--     (warn/throttle/reserve) only exist once a row's fractions are set.
--   * No org-level table is added: the org/key ceiling is modeled as an ordinary
--     `budgets` row under the RESERVED SENTINEL workstream `__org__` (see below).
--
-- `warn_frac`/`throttle_frac`/`reserve_frac` are spent-fraction (spent+estimate
-- over cap) thresholds in (0,1), ordered warn <= throttle <= reserve. Suggested
-- defaults live in runtime/budget.py (0.70 / 0.85 / 0.90) but are applied only
-- when a caller opts in — the schema never back-fills them (keeps back-compat).
--
-- Two-level ceiling (documented convention, no schema change): a per-workstream
-- model call is checked against BOTH its own allocation row AND the org ceiling
-- row `('__org__', period)`. The org row's "spent" is the SUM across ALL
-- workstreams in the period (runtime/budget.py `org_spent`), so it is a true
-- org/key ceiling. The TIGHTER of the two wins. A workstream with no allocation
-- row and no `__org__` row is unconstrained, exactly as before.
--
-- Forward-only and idempotent (like 0010/0012): ADD COLUMN IF NOT EXISTS is a
-- no-op on re-run and each constraint is guarded by a pg_constraint existence
-- check; the migration runner also skips already-applied files.

ALTER TABLE budgets ADD COLUMN IF NOT EXISTS warn_frac     real;  -- (0,1) NULL = hard-cap only
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS throttle_frac real;  -- (0,1) NULL = hard-cap only
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS reserve_frac  real;  -- (0,1) NULL = hard-cap only

DO $$ BEGIN
    -- Each fraction, when set, is a strict spent-fraction in the open range (0,1):
    -- 0 would warn immediately and 1 would coincide with the hard cap.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'budgets_fracs_range') THEN
        ALTER TABLE budgets ADD CONSTRAINT budgets_fracs_range CHECK (
            (warn_frac     IS NULL OR (warn_frac     > 0 AND warn_frac     < 1))
        AND (throttle_frac IS NULL OR (throttle_frac > 0 AND throttle_frac < 1))
        AND (reserve_frac  IS NULL OR (reserve_frac  > 0 AND reserve_frac  < 1))
        );
    END IF;
    -- Zones must nest: warn <= throttle <= reserve (a NULL bound is unordered).
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'budgets_fracs_order') THEN
        ALTER TABLE budgets ADD CONSTRAINT budgets_fracs_order CHECK (
            (warn_frac     IS NULL OR throttle_frac IS NULL OR warn_frac     <= throttle_frac)
        AND (throttle_frac IS NULL OR reserve_frac  IS NULL OR throttle_frac <= reserve_frac)
        );
    END IF;
END $$;
