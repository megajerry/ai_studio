-- 0019 — free-form training-data store: relocate embedded free-text OUT of the
-- append-only event log (ADR-0032; reconciles invariants #6 body-free events &
-- #7 local-first with ADR-0011/0012/0020's "bodies are LOCAL DB ONLY").
--
-- Several emit sites historically embedded FREE-FORM TEXT in `events.payload`:
--   - pm.pushback / pm.needs_clarification / pm.consensus / pm.planned carried the
--     verbatim `goal` objective (and pushback/clarification a free-text `reason`);
--   - verify.passed / verify.failed carried the MODEL-authored `verdict.reason`;
--   - work.retry carried the same model-authored `verdict.reason`.
-- That text is NOT redacted (the stakeholder wants it kept) — it is RELOCATED here,
-- explicitly retained as self-improvement TRAINING DATA. The event log stays
-- body-free (ids/types/counts only, invariant #6); the full free-text lives in the
-- LOCAL DB ONLY (invariant #7), exactly like trajectory bodies (0011 / ADR-0020).
--
-- All writes go through the single guarded writer runtime/free_form.py (parameterized
-- SQL only — no ad-hoc INSERT/UPDATE elsewhere), mirroring runtime/trajectory.py.
--
-- NOTE (approvals.reason exception): the short bounded `reason` on approval.requested
-- is DOCUMENTED as intentional/bounded (ADR-0006) and stays on that event — it is not
-- relocated here (see ADR-0032). This store is for the unbounded free-text above.
--
-- Forward-only and idempotent (like 0006/0008/0009/0011): CREATE ... IF NOT EXISTS is
-- a no-op on re-run and the migration runner skips already-applied files anyway.
--
-- No FKs to tasks/trajectories on purpose: training data must OUTLIVE the task and
-- the trajectory TTL (a trajectory expiry or a deleted task must never take the
-- retained training text with it). Links are plain, indexed correlation columns.

CREATE TABLE IF NOT EXISTS event_free_form (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Link keys back to the event whose payload this text was relocated from. The
    -- guarded writer always records (event_type, workstream) and, when known, the
    -- task_id / trajectory_id; event_seq is nullable (the EventSink write path does
    -- not surface the assigned seq) but retained for callers that can supply it.
    event_seq     bigint,
    task_id       uuid,
    trajectory_id uuid,
    event_type    text NOT NULL,       -- the event type the text came from (e.g. 'pm.planned')
    workstream    text NOT NULL,
    -- Closed vocabulary (mirrored by runtime/free_form.py KINDS):
    --   goal      — a PM objective / restated-goal string
    --   reason    — a PM plan reason (pushback / needs_clarification)
    --   rationale — MODEL-authored verifier prose (verify.* / work.retry verdict.reason)
    kind          text NOT NULL CHECK (kind IN ('goal','reason','rationale')),
    content       text NOT NULL,       -- FULL free-form text: LOCAL DB ONLY, never on the wire
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Retrieval-as-training-data: "all `rationale` in creation order", etc.
CREATE INDEX IF NOT EXISTS event_free_form_kind_created_idx
    ON event_free_form (kind, created_at);
-- Resolve the free-form text tied to a specific task (linkage / per-task training set).
CREATE INDEX IF NOT EXISTS event_free_form_task_idx
    ON event_free_form (task_id);
-- Scan the text relocated from one event type across the studio.
CREATE INDEX IF NOT EXISTS event_free_form_type_idx
    ON event_free_form (event_type);
