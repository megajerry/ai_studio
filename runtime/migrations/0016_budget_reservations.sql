-- 0016 — budget reservations: close the enforce() TOCTOU race (ADR-0006/0022).
--
-- `runtime/budget.py::enforce` used to read a workstream's accrued spend (from the
-- append-only `model.call` events) and decide a zone WITHOUT taking a lock or
-- reserving anything. Under concurrency N in-flight calls near the cap all read
-- the SAME stale `spent()` and all passed the gate — their combined spend then
-- blew past the ceiling (a check-then-act / TOCTOU race that violates "budget
-- caps aren't exceeded"). Real accrued spend is still the single source of cost
-- truth (ADR-0012); these two columns add only an in-flight CUSHION so concurrent
-- pre-checks serialize on the row and SEE each other's not-yet-recorded estimates.
--
-- `reserved_usd` / `reserved_tokens` hold the sum of estimates for calls that have
-- passed `enforce` but whose real `model.call` has not landed yet. `enforce` takes
-- `SELECT ... FOR UPDATE` on the row, gates on `spent + reserved + estimate` vs the
-- cap, and — only if it fits — INCREMENTS the reservation before releasing the lock.
-- The reservation is RELEASED (decremented by the same estimate) once the real
-- spend is recorded (or the call fails/aborts), so it never permanently shrinks the
-- cap. The zone/threshold math (ADR-0022) counts the reservation toward the
-- fraction, so warn/throttle/reserve/over stay consistent under concurrency.
--
-- ADDITIVE + BACK-COMPATIBLE: both columns are NOT NULL DEFAULT 0, so every
-- existing row starts with a zero cushion and single-call behavior is unchanged
-- (reserved == 0 ⇒ `spent + 0 + est` == the old predicate). Forward-only and
-- idempotent (like 0010/0013): ADD COLUMN IF NOT EXISTS is a no-op on re-run and
-- the migration runner also skips already-applied files.

ALTER TABLE budgets ADD COLUMN IF NOT EXISTS reserved_usd    numeric NOT NULL DEFAULT 0;
ALTER TABLE budgets ADD COLUMN IF NOT EXISTS reserved_tokens bigint  NOT NULL DEFAULT 0;

DO $$ BEGIN
    -- A reservation is a non-negative cushion; release floors it at 0 (GREATEST),
    -- so a leaked/over-release can never drive it negative and corrupt the gate.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'budgets_reserved_nonneg') THEN
        ALTER TABLE budgets ADD CONSTRAINT budgets_reserved_nonneg CHECK (
            reserved_usd >= 0 AND reserved_tokens >= 0
        );
    END IF;
END $$;
