"""Canonical event-type strings — the single source of truth for ``events.type``.

Every subsystem's event-type wire string is defined here exactly once, so
producers and consumers agree on the bytes written to the free-form
``events.type`` column. Modules import the ``EVENT_*`` constants (and the M1
:class:`EventType` enum) from here instead of re-declaring their own; the enum
is also re-exported from :mod:`runtime.models` for backward-compatible imports.

This module is dependency-free (stdlib only) so anything — including the
DB-free ``runtime.adaptive`` planner — can import it without pulling in psycopg.
Values are byte-identical to the historical per-module definitions: changing one
here is a wire/telemetry change, not a refactor.
"""

from __future__ import annotations

from enum import Enum

# --- M1 task lifecycle (owned by runtime.tasks / runtime.supervisor) ---------
EVENT_TASK_CREATED = "task.created"
EVENT_TASK_CLAIMED = "task.claimed"
EVENT_TASK_HEARTBEAT = "task.heartbeat"
EVENT_TASK_FINISHED = "task.finished"
# Every guarded state change (runtime.tasks.transition) emits this with the
# from/to statuses, acting agent + type, and latency since the previous
# transition — the append-only lifecycle telemetry (ADR-0012/0015).
EVENT_TASK_TRANSITION = "task.transition"
# Emitted by the non-agent supervisor (ADR-0004) when it re-kicks a task whose
# worker went stale, or force-fails one that exhausted its retries.
EVENT_TASK_REKICKED = "task.rekicked"
EVENT_TASK_FAILED_EXHAUSTED = "task.failed_exhausted"
# --- Graduated recovery ladder (runtime.supervisor, ADR-0023) ---------------
#: The cheapest recovery rung: on the FIRST detection of a stall the supervisor
#: emits this and defers the re-kick for a short grace window so a transient stall
#: can recover with its in-flight progress preserved (no reset). BODY-FREE:
#: carries only ids/status + the grace window seconds (invariants 5 & 6).
EVENT_TASK_NUDGE = "task.nudge"
#: The progress-aware escalation SIGNAL: re-kicks have made NO net progress up to
#: the stuck threshold, so the supervisor STOPS re-kicking (before exhausting
#: retries) and supersedes the attempt for PM re-decomposition (R2 consumes this).
#: BODY-FREE: carries only ids/status + a stall reason CODE + counts — never body text.
EVENT_TASK_STUCK = "task.stuck"
#: The PM's response to a ``task.stuck`` signal (ADR-0023, R2): it re-decomposed the
#: superseded (abandoned) task into N SMALLER subtasks. BODY-FREE: carries only the
#: original task id + the new subtask ids + count + replan depth — never body text.
EVENT_TASK_REPLANNED = "task.replanned"
#: The bounded-replan backstop (ADR-0023, R2): a task that stayed stuck past the max
#: replan depth is escalated to a human 🛑 instead of re-decomposing again (no
#: infinite replan). BODY-FREE: carries only the original task id + depth/cap +
#: approval id — never body text.
EVENT_TASK_REPLAN_ESCALATED = "task.replan_escalated"

# --- Model-call failure telemetry (runtime.model.call, ADR-0023) ------------
#: A provider raised (other than the handled ProviderFallback) during a model
#: call, so an API-error death becomes attributable telemetry (R3 consumes this).
#: BODY-FREE: carries ONLY the error CLASS/type name + model/provider/role/task_id
#: — NEVER prompt/response/secret text (invariants 5 & 6).
EVENT_MODEL_CALL_FAILED = "model.call.failed"

# --- Worker orchestration (runtime.worker) ----------------------------------
#: Re-enqueue of a work task after a verify fail.
EVENT_WORK_RETRY = "work.retry"
#: A retro was enqueued after a terminal work task.
EVENT_RETRO_TRIGGERED = "retro.triggered"
#: A review was enqueued after a terminal work task.
EVENT_REVIEW_TRIGGERED = "review.triggered"
#: A task blocked on a 🔴 approval was re-queued after a grant.
EVENT_APPROVAL_RESUMED = "approval.resumed"

# --- Approval loop (runtime.approvals, enforced by runtime.enforce) ----------
EVENT_APPROVAL_REQUESTED = "approval.requested"
EVENT_APPROVAL_RESOLVED = "approval.resolved"

# --- Policy enforcement (runtime.enforce) -----------------------------------
EVENT_POLICY_DECISION = "policy.decision"
EVENT_TOOL_INVOKED = "tool.invoked"

# --- Budget (runtime.budget) ------------------------------------------------
EVENT_BUDGET_EXCEEDED = "budget.exceeded"
EVENT_BUDGET_CHECKPOINT = "budget.checkpoint"
# --- Graduated capacity governance (runtime.budget, ADR-0022) ---------------
#: Tiered-threshold telemetry. All BODY-FREE: payloads carry ONLY amounts +
#: workstream/period + zone (+ purpose on reserve) — never prompts, args, secrets
#: (invariants 5 & 6). warn/throttle are NON-BLOCKING (the call proceeds); reserve
#: is emitted when the workstream has entered the reserve buffer near its cap — a
#: `normal` call is then WITHHELD (buffer preserved) while `wind_down`/`escalation`
#: is allowed through, so a workstream can react/pivot/escalate BEFORE breaching.
EVENT_BUDGET_WARN = "budget.warn"
EVENT_BUDGET_THROTTLE = "budget.throttle"
EVENT_BUDGET_RESERVE = "budget.reserve"

# --- Capacity Steward (runtime.roles.capacity_steward, ADR-0022 C2) ----------
#: The optional Capacity Steward's reviewable recommendation telemetry — the
#: BEHAVIORAL layer on top of the deterministic budget engine (C1). BODY-FREE:
#: payloads carry ONLY ids / workstream / period / zone / amounts + a recommended
#: ACTION enum — never prompts, args, or secrets (invariants 5 & 6). The Steward
#: MONITORS burn + FLAGS a projected breach EARLY + RECOMMENDS an action; it NEVER
#: enforces (the engine does, via `budget.enforce`) and NEVER raises a ceiling
#: (that stays a 🛑 PM/stakeholder decision, ADR-0006). ``capacity.flagged`` = a
#: workstream is projected to breach before its period ends; ``capacity.recommendation``
#: = the steward's suggested action (compact/pivot/reallocate/escalate).
EVENT_CAPACITY_FLAGGED = "capacity.flagged"
EVENT_CAPACITY_RECOMMENDATION = "capacity.recommendation"

# --- Memory (runtime.memory) ------------------------------------------------
EVENT_MEMORY_REMEMBERED = "memory.remembered"
EVENT_MEMORY_RECALLED = "memory.recalled"

# --- Model routing + calls (runtime.model) ----------------------------------
EVENT_MODEL_ROUTED = "model.routed"
EVENT_MODEL_CALL = "model.call"

# --- Search gateway (runtime.search) ----------------------------------------
EVENT_SEARCH_DENIED = "search.denied"
EVENT_SEARCH_CACHE_HIT = "search.cache_hit"
EVENT_SEARCH_CACHE_MISS = "search.cache_miss"
EVENT_SEARCH_PROVIDER_CALL = "search.provider_call"

# --- Cross-workstream feature requests (runtime.crossworkstream) -------------
EVENT_REQUEST_SUBMITTED = "request.submitted"
EVENT_REQUEST_UNDER_REVIEW = "request.under_review"
EVENT_REQUEST_ACCEPTED = "request.accepted"
EVENT_REQUEST_DECLINED = "request.declined"
EVENT_REQUEST_NEEDS_CLARIFICATION = "request.needs_clarification"
EVENT_REQUEST_ESCALATED = "request.escalated"

# --- Roles ------------------------------------------------------------------
#: PM (runtime.roles.pm) — the three confidence-gate outcomes.
EVENT_PM_PLANNED = "pm.planned"
EVENT_PM_NEEDS_CLARIFICATION = "pm.needs_clarification"
EVENT_PM_PUSHBACK = "pm.pushback"
#: PM↔Critic consensus loop outcome (runtime.roles.pm) — rounds + outcome only.
EVENT_PM_CONSENSUS = "pm.consensus"
#: Critic (runtime.roles.critic) — a forward-looking critique of a decision.
EVENT_CRITIC_REVIEWED = "critic.reviewed"
#: Executor (runtime.roles.executor).
EVENT_EXECUTOR_ACTED = "executor.acted"
#: Verifier (runtime.roles.verifier) — the verify→commit decision.
EVENT_VERIFY_PASSED = "verify.passed"
EVENT_VERIFY_FAILED = "verify.failed"
#: Reviewer (runtime.roles.reviewer) — verdict + the 🚨 HIGH-severity alarm.
EVENT_REVIEW_PASSED = "review.passed"
EVENT_REVIEW_FLAGGED = "review.flagged"
EVENT_REVIEW_ALARM = "review.alarm"
#: Retro (runtime.roles.retro).
EVENT_RETRO_COMPLETED = "retro.completed"
#: Researcher (runtime.roles.researcher).
EVENT_RESEARCH_COMPLETED = "research.completed"
#: Sourcing (runtime.roles.sourcing).
EVENT_SOURCING_PROPOSED = "sourcing.proposed"
EVENT_SOURCING_AUTOADOPTED = "sourcing.autoadopted"
#: Failure-pattern analyst (runtime.roles.failure_analyst, ADR-0023 R3). These are
#: BODY-FREE: payloads carry ONLY a pattern id / kind / the error_type|stall_reason
#: CODE / the rate + sample size + Wilson CI / thresholds / ids — NEVER prompt,
#: response, or any secret/body text (invariants 5 & 6, mirroring model.call.failed).
#: ``failure.pattern_detected`` = a RECURRING failure pattern crossed the detection
#: bound (CI lower bound > threshold AND n ≥ floor); ``fix.proposed`` = a durable-fix
#: candidate was written to a review path + registered as an experiment.proposed
#: (the fix is NEVER auto-applied — a human applies it and the experiment then
#: watches real post-fix traffic to confirm/deny effectiveness).
EVENT_FAILURE_PATTERN_DETECTED = "failure.pattern_detected"
EVENT_FIX_PROPOSED = "fix.proposed"

# --- Skill attribution telemetry (runtime.skills.inject, ADR-0024) -----------
#: Emitted whenever a REVIEWED skill is injected into a role's prompt (the P0
#: attribution hook of the skill-efficacy foundation). BODY-FREE: the payload
#: carries ONLY the injected skill NAME(s) + the acting role; the task_id lives on
#: the envelope. It NEVER carries a skill's instruction body/resources/prompt text
#: (invariants 5 & 6, mirroring the trajectory.* / model.call.failed discipline) —
#: names are identifiers, not bodies. This is what makes per-skill usage
#: attributable in the event log so ``skill_efficacy_report`` can compare an
#: applied cohort against a baseline. An unreviewed/skipped skill is NOT injected,
#: so it emits NO ``skill.applied``.
EVENT_SKILL_APPLIED = "skill.applied"

# --- Trajectory observability (runtime.trajectory) --------------------------
#: The reasoning-trajectory writer (ADR-0020). These are BODY-FREE: payloads carry
#: ONLY ids / types / seq / step_type / counts — NEVER rationale, summary, goal, or
#: outcome text (those bodies live in the local DB only; invariants 5 & 6).
EVENT_TRAJECTORY_STARTED = "trajectory.started"
EVENT_TRAJECTORY_STEP_ADDED = "trajectory.step_added"
EVENT_TRAJECTORY_CLOSED = "trajectory.closed"
EVENT_TRAJECTORY_COMPACTED = "trajectory.compacted"
EVENT_TRAJECTORY_EXPIRED = "trajectory.expired"

# --- Spokesman grounding + trust ledger (runtime.trust) ---------------------
#: Human-facing comms grounding + accountability (ADR-0021). These are BODY-FREE:
#: payloads carry ONLY ids / identity / status / kind / strikes counts — NEVER the
#: claim `statement` text (that lives in the local `comms_claims` table only;
#: invariants 5 & 6, mirroring the trajectory.* discipline above).
#: A factual claim the Spokesman gate verified / rejected against its evidence.
EVENT_COMMS_CLAIM_VERIFIED = "comms.claim_verified"
EVENT_COMMS_CLAIM_REJECTED = "comms.claim_rejected"
#: A factual claim the Spokesman gate could NOT resolve against source of truth
#: (missing proof — NOT a fabrication). The gate withholds it and asks the
#: originating identity for proof. Body-free: carries ONLY claim_id + identity.
EVENT_COMMS_PROOF_REQUESTED = "comms.proof_requested"
#: The worst offense — a fabrication (false info relayed as fact) was detected;
#: 🚨 escalated (ADR-0006) and paired with the trust penalty below.
EVENT_COMMS_FABRICATION_DETECTED = "comms.fabrication_detected"
#: Trust-ledger penalty telemetry (zero-tolerance): a strike was recorded, and the
#: identity's human-facing-relay capability was permanently revoked.
EVENT_TRUST_STRIKE = "trust.strike"
EVENT_TRUST_CAPABILITY_REVOKED = "trust.capability_revoked"

# --- Experiments (runtime.experiment) ---------------------------------------
EVENT_PROPOSED = "experiment.proposed"
EVENT_STARTED = "experiment.started"
EVENT_OBSERVED = "experiment.observation"
EVENT_EVALUATED = "experiment.evaluated"


class EventType(str, Enum):
    """M1 task-lifecycle event types (ADR-0012/0015).

    Kept as an enum so the typed lifecycle path (runtime.tasks) has a closed
    vocabulary; the ``events.type`` column itself is free-form text so other
    subsystems emit their own types via the ``EVENT_*`` constants above. Values
    are built from those constants so each wire string is written exactly once.
    """

    TASK_CREATED = EVENT_TASK_CREATED
    TASK_CLAIMED = EVENT_TASK_CLAIMED
    TASK_HEARTBEAT = EVENT_TASK_HEARTBEAT
    TASK_FINISHED = EVENT_TASK_FINISHED
    TASK_TRANSITION = EVENT_TASK_TRANSITION
    TASK_REKICKED = EVENT_TASK_REKICKED
    TASK_FAILED_EXHAUSTED = EVENT_TASK_FAILED_EXHAUSTED
    TASK_NUDGE = EVENT_TASK_NUDGE
    TASK_STUCK = EVENT_TASK_STUCK
