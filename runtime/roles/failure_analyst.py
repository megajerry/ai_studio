"""Failure-pattern analyst — recognize a RECURRING failure → propose a durable fix
→ frame it as an experiment watched on real traffic (ADR-0023 R3).

This closes the loop the stakeholder asked for. The R1 telemetry made API-error
worker deaths attributable (body-free ``model.call.failed`` carrying the error
CLASS) and made stalls attributable (``task.stuck`` carrying a ``stall_reason``
CODE). This role reads that telemetry (via :func:`runtime.quality.failure_report`),
recognizes when ONE failure kind is *recurring* — not a one-off — and:

1. **PROPOSES a durable-fix candidate** (a written proposal artifact through the
   policy-gated filesystem tool — "harden the model call: retry+backoff /
   circuit-breaker / idempotent checkpoint for ``error_type=X``"). It NEVER applies
   the fix; the artifact is a reviewable candidate, exactly the
   :mod:`runtime.roles.sourcing` discipline.
2. **Registers the fix as an ``experiment.proposed``** with a hypothesis + a target
   metric ("the pattern's rate should drop at/below the alarm threshold"), so that
   once a human APPLIES the fix and starts observing, the experiment reads real
   POST-FIX traffic (via ``failure_report`` scoped to the post-fix ``seq`` window)
   and :func:`runtime.experiment.evaluate_experiment` confirms/denies effectiveness
   from facts — never a claim.

It acts through exactly the sanctioned seams — never agent-direct (architecture §9,
CLAUDE.md invariants 1-3):

- **Reads only the append-only event log** through :func:`runtime.quality.failure_report`.
- **Any file write via the policy-gated tool layer** — the proposal is written via
  ``invoke(role="failure_analyst", tool_name="filesystem", op="write", …)`` to a
  review path (``proposals/fixes/<pattern>.md`` under the confined tool root;
  git-ignored). A role without ``fs.write`` is DENIED (nothing written — a safe
  no-op, mirroring Sourcing).
- **Frames the bet only via the EXPERIMENT primitive** (:mod:`runtime.experiment`) —
  ``propose_experiment`` puts the fix in ``proposed`` state; it does NOT ``start`` it
  (starting/observing/evaluating happens after a human applies the fix).

Invariants it upholds:

- **Never fires on a tiny sample.** A pattern is detected ONLY when the Wilson 95%
  CI LOWER bound exceeds the threshold AND ``n`` ≥ a floor — a "1.0 on n=3" is noise,
  not a recurring failure (statistical-rigor doctrine).
- **Never auto-applies a fix + no loop.** It writes a candidate + proposes an
  experiment and stops. It enqueues NOTHING and starts NO experiment (no fix code
  runs; that is a separate, human-gated task).
- **Events leak nothing.** ``failure.pattern_detected`` / ``fix.proposed`` carry a
  pattern id / kind / the error_type|stall_reason CODE / the rate + n + Wilson CI /
  thresholds / ids only — never prompt/response/secret/body text (invariants 5 & 6).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..event_types import EVENT_FAILURE_PATTERN_DETECTED, EVENT_FIX_PROPOSED
from ..experiment import (
    Experiment,
    SuccessMetric,
    evaluate_experiment as _evaluate_experiment,
    get_experiment as _get_experiment,
    propose_experiment as _propose_experiment,
    record_observation as _record_observation,
)
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..quality import failure_report as _failure_report
from ..tools import ToolRegistry

log = logging.getLogger("runtime.roles.failure_analyst")

#: The queue task types the worker dispatches to :func:`run_failure_analysis`.
FAILURE_ANALYST_TASK_TYPES = ("analyze.failures", "failure_analysis")

#: The role name the policy gate checks (must be granted ``fs.write`` to write the
#: reviewable proposal; without it the write is DENIED — a safe, logged no-op).
ROLE = "failure_analyst"

#: Pattern KINDS — which telemetry series a recurring failure was recognized in.
KIND_MODEL_CALL_ERROR = "model_call_error"  # a recurring model.call.failed error_type
KIND_TASK_STALL = "task_stall"              # a recurring task.stuck stall_reason

#: Detection defaults. A pattern fires only when the CI LOWER bound exceeds
#: ``threshold`` AND ``n`` ≥ ``min_sample`` — so a tiny/lucky sample never triggers.
DEFAULT_THRESHOLD = 0.2
#: Minimum sample floor. Aligned with the workstream trustworthy-sample threshold so
#: a rate below it is never treated as a recurring pattern (statistical-rigor).
DEFAULT_MIN_SAMPLE = 30

#: The prefix that marks a fix experiment's success metric (parsed back at eval).
METRIC_PREFIX = "failure_rate"

#: Default review directory (under the confined tool root; git-ignored). NOT a live
#: code path — applying a proposal is a separate, reviewed, human-gated step.
DEFAULT_PROPOSALS_DIR = "proposals/fixes"

#: Hard cap on patterns proposed per task — bounds fan-out (one proposal each).
MAX_PATTERNS = 4

#: The durable-fix hardening menu a proposal offers for review (the stakeholder's
#: ask). These are DESCRIPTIONS in a reviewable artifact, never applied here.
FIX_KINDS = ("retry_backoff", "circuit_breaker", "idempotent_checkpoint")


# ===========================================================================
# Pure detection + parsing (no DB / no model / no network) — unit-testable
# ===========================================================================


class FailurePattern(BaseModel):
    """A recurring failure recognized in the telemetry (the detector's output).

    Carries the CODE that identifies the failure (an ``error_type`` CLASS or a
    ``stall_reason`` CODE), the proportion it occurs at, and — crucially — the sample
    size ``n`` and Wilson 95% CI, so the evidence travels with the estimate.
    """

    pattern_id: str  # "<kind>:<key>", e.g. "model_call_error:RateLimitError"
    kind: str        # KIND_MODEL_CALL_ERROR | KIND_TASK_STALL
    key: str         # the error_type CLASS or stall_reason CODE
    rate: float
    successes: int
    n: int
    ci95: tuple[float, float]
    threshold: float
    min_sample: int


def _fires(share: dict, *, threshold: float, min_sample: int) -> bool:
    """A recurrence test that CANNOT be tricked by a tiny sample.

    Fires only when the sample is large enough (``n`` ≥ ``min_sample``) AND the
    Wilson 95% CI LOWER bound clears ``threshold`` — a point estimate alone (even a
    perfect 1.0) never fires on a small ``n`` because its lower bound stays low.
    """
    n = int(share["n"])
    ci = share["ci95"]
    return n >= min_sample and ci is not None and ci[0] > threshold


def detect_patterns(
    report: dict,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> list[FailurePattern]:
    """Recognize recurring failures in a :func:`runtime.quality.failure_report`.

    Pure + deterministic. Scans the ``by_error_type`` and ``by_stall_reason``
    breakdowns; a category becomes a :class:`FailurePattern` only when
    :func:`_fires` holds (CI lower bound > ``threshold`` AND ``n`` ≥ ``min_sample``).
    Returned strongest-evidence-first (by CI lower bound) and bounded to
    :data:`MAX_PATTERNS`.
    """
    found: list[FailurePattern] = []
    for kind, entries, key_field in (
        (KIND_MODEL_CALL_ERROR, report.get("by_error_type", []), "error_type"),
        (KIND_TASK_STALL, report.get("by_stall_reason", []), "stall_reason"),
    ):
        for e in entries:
            share = e["share"]
            key = e.get(key_field)
            if not key or not _fires(share, threshold=threshold, min_sample=min_sample):
                continue
            found.append(
                FailurePattern(
                    pattern_id=f"{kind}:{key}",
                    kind=kind,
                    key=key,
                    rate=float(share["rate"]),
                    successes=int(share["successes"]),
                    n=int(share["n"]),
                    ci95=tuple(share["ci95"]),  # type: ignore[arg-type]
                    threshold=threshold,
                    min_sample=min_sample,
                )
            )
    found.sort(key=lambda p: (p.ci95[0], p.rate), reverse=True)
    return found[:MAX_PATTERNS]


def metric_name_for(pattern_id: str) -> str:
    """The success-metric name a fix experiment for ``pattern_id`` reads."""
    return f"{METRIC_PREFIX}:{pattern_id}"


def pattern_rate_from_report(report: dict, metric_name: str) -> Optional[float]:
    """Read a fix experiment's target metric back out of a ``failure_report``.

    Inverts :func:`metric_name_for`: parses ``failure_rate:<kind>:<key>`` and returns
    the CURRENT proportion of that failure over the report's window — the same
    denominator the detector used (all model calls for an error_type; terminal tasks
    for a stall_reason). ``None`` when the denominator is 0 (no post-fix traffic yet
    → no evidence → the experiment is killed, never kept on faith). This is the wire
    that makes ``experiment.evaluated`` a REAL-traffic verdict.
    """
    prefix, _, pattern_id = metric_name.partition(":")
    if prefix != METRIC_PREFIX or not pattern_id:
        raise ValueError(f"not a failure-fix metric: {metric_name!r}")
    kind, _, key = pattern_id.partition(":")
    if kind == KIND_MODEL_CALL_ERROR:
        total = int(report["totals"]["model_calls_total"])
        succ = sum(int(e["count"]) for e in report.get("by_error_type", [])
                   if e.get("error_type") == key)
    elif kind == KIND_TASK_STALL:
        total = int(report["totals"]["tasks_terminal"])
        succ = sum(int(e["count"]) for e in report.get("by_stall_reason", [])
                   if e.get("stall_reason") == key)
    else:
        raise ValueError(f"unknown pattern kind {kind!r} in metric {metric_name!r}")
    return round(succ / total, 4) if total else None


def render_fix_proposal(pattern: FailurePattern, *, metric_name: str, target: float) -> str:
    """Render the reviewable durable-fix proposal (Markdown) — the runtime PR analogue.

    Describes the RECURRING failure with its evidence (rate + n + CI), offers the
    hardening menu (:data:`FIX_KINDS`), and states the experiment's success gate. It
    is a candidate for REVIEW — applying it is a separate, human-gated step.
    """
    lo, hi = pattern.ci95
    which = "error_type" if pattern.kind == KIND_MODEL_CALL_ERROR else "stall_reason"
    return (
        f"# PROPOSED durable fix (CANDIDATE — NOT applied) — {pattern.pattern_id}\n\n"
        "Produced by the Failure-pattern analyst (ADR-0023 R3). REVIEW before applying;\n"
        "applying the fix is a separate, human-gated task. This artifact only proposes.\n\n"
        "## Recurring failure recognized\n\n"
        f"- kind: `{pattern.kind}`\n"
        f"- {which}: `{pattern.key}`\n"
        f"- observed rate: {pattern.rate} ({pattern.successes}/{pattern.n})\n"
        f"- Wilson 95% CI: [{lo}, {hi}] (fired: CI lower {lo} > threshold {pattern.threshold},\n"
        f"  and n={pattern.n} ≥ floor {pattern.min_sample})\n\n"
        "## Proposed durable fix (harden the failing call)\n\n"
        "- **retry_backoff** — bounded retries with exponential backoff + jitter for the\n"
        "  transient class, so a recoverable provider error does not kill the worker.\n"
        "- **circuit_breaker** — trip after consecutive failures to stop hammering a\n"
        "  degraded provider; fall back to the router's next candidate.\n"
        "- **idempotent_checkpoint** — checkpoint progress so a retry resumes instead of\n"
        "  redoing work (no duplicate side effects).\n\n"
        "## Verify as an experiment (real post-fix traffic)\n\n"
        f"- success metric: `{metric_name}` `<=` `{target}` (aggregate: last)\n"
        f"- hypothesis: applying the fix drops `{pattern.key}` at/below {target}.\n"
        "- evaluation reads POST-FIX traffic from the event log (failure_report scoped\n"
        "  to the seq cursor captured when the fix is applied) — a fact, not a claim.\n"
    )


# ===========================================================================
# Result model
# ===========================================================================


class ProposedFix(BaseModel):
    """One recurring pattern → its proposal artifact + its framing experiment."""

    pattern_id: str
    kind: str
    key: str
    rate: float
    n: int
    ci95: tuple[float, float]
    #: Proposal-write outcome: "off" | "executed" | "denied" | "pending".
    proposal_status: str = "off"
    #: Path (tool-root-relative) of the written proposal, if written.
    proposal_path: Optional[str] = None
    #: The registered experiment.proposed id (framing the verify-as-experiment).
    experiment_id: Optional[str] = None
    metric_name: str
    target: float


class FailureAnalysisResult(BaseModel):
    """What one failure-analysis task produced (returned to the worker as the result).

    Counts / ids / rates only — never bodies (invariants 5 & 6). ``patterns_detected``
    is 0 on a healthy/quiet workstream (nothing proposed, nothing emitted).
    """

    workstream: str
    patterns_detected: int
    proposals: list[ProposedFix] = Field(default_factory=list)
    threshold: float
    min_sample: int


# ===========================================================================
# DB-integrated role — detect → propose candidate → register experiment
# ===========================================================================


def _write_proposal(
    conn: Any,
    task: Task,
    content: str,
    path: str,
    *,
    tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig],
    sink: EventSink,
    invoke_fn: Callable[..., Any],
) -> tuple[str, Optional[str]]:
    """Write the reviewable fix proposal via the policy-gated filesystem tool.

    Returns ``(status, path)``; a role without ``fs.write`` is DENIED (nothing
    written) — a safe, logged no-op, mirroring :func:`runtime.roles.sourcing`.
    """
    result = invoke_fn(
        role=ROLE,
        tool_name="filesystem",
        registry=tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=path,
        content=content,
    )
    status = getattr(result.status, "value", str(result.status))
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    return status, (path if wrote else None)


def run_failure_analysis(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    tool_registry: Optional[ToolRegistry] = None,
    policy: Optional[PolicyConfig] = None,
    threshold: float = DEFAULT_THRESHOLD,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    target_rate: Optional[float] = None,
    proposals_dir: str = DEFAULT_PROPOSALS_DIR,
    since_seq: Optional[int] = None,
    failure_report: Callable[..., dict] = _failure_report,
    propose_experiment: Callable[..., Any] = _propose_experiment,
    invoke_fn: Callable[..., Any] = invoke,
) -> FailureAnalysisResult:
    """Service one failure-analysis task: detect recurring failure → propose fix + experiment.

    Reads :func:`runtime.quality.failure_report` for ``task.workstream``, recognizes
    recurring patterns (:func:`detect_patterns` — CI lower bound > ``threshold`` AND
    ``n`` ≥ ``min_sample``), and for each (bounded to :data:`MAX_PATTERNS`): writes a
    reviewable durable-fix proposal via the policy-gated filesystem tool, registers an
    ``experiment.proposed`` whose target metric (``<= target_rate``, default
    ``threshold``) will be read from real post-fix traffic, and emits body-free
    ``failure.pattern_detected`` + ``fix.proposed`` events.

    It NEVER applies a fix, NEVER starts the experiment, and enqueues NOTHING (no
    loop). ``threshold`` / ``min_sample`` / ``target_rate`` may be overridden from
    ``task.payload``. Injectable seams (``failure_report`` / ``propose_experiment`` /
    ``invoke_fn``) keep it testable; ``policy`` gates the proposal write.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    threshold = float(payload.get("threshold", threshold))
    min_sample = int(payload.get("min_sample", min_sample))
    if "target_rate" in payload:
        target_rate = float(payload["target_rate"])
    target = float(target_rate) if target_rate is not None else threshold

    report = failure_report(conn, task.workstream, since_seq=since_seq)
    patterns = detect_patterns(report, threshold=threshold, min_sample=min_sample)

    proposals: list[ProposedFix] = []
    for pat in patterns:
        metric_name = metric_name_for(pat.pattern_id)

        # 1. Write the reviewable durable-fix proposal (never applied; denied cleanly
        #    without fs.write). Path is derived from the pattern id (no ':' in paths).
        proposal_status = "off"
        proposal_path: Optional[str] = None
        if tool_registry is not None:
            safe = pat.pattern_id.replace(":", "__").replace("/", "_")
            content = render_fix_proposal(pat, metric_name=metric_name, target=target)
            proposal_status, proposal_path = _write_proposal(
                conn, task, content, f"{proposals_dir}/{safe}.md",
                tool_registry=tool_registry, policy=policy, sink=sink, invoke_fn=invoke_fn,
            )

        # 2. Register the fix as an experiment.proposed — the verify-as-experiment
        #    framing. Lower-is-better: the pattern's rate should drop to <= target.
        #    We PROPOSE only (never start) — a human applies the fix, then observes.
        experiment_id: Optional[str] = None
        if conn is not None:
            exp = propose_experiment(
                conn,
                workstream=task.workstream,
                hypothesis=(
                    f"Applying a durable fix ({'/'.join(FIX_KINDS)}) for "
                    f"{pat.kind} '{pat.key}' drops its rate to <= {target} on real "
                    f"post-fix traffic (was {pat.rate} over n={pat.n})."
                ),
                metric=SuccessMetric(
                    name=metric_name, target=target, comparator="<=", aggregate="last"
                ),
                sink=sink,
            )
            experiment_id = str(exp.id)

        # 3. Emit body-free telemetry: the recognized pattern + the framing.
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_FAILURE_PATTERN_DETECTED,
                task_id=task.id,
                payload={
                    "pattern_id": pat.pattern_id,
                    "kind": pat.kind,
                    "pattern_key": pat.key,
                    "error_type": pat.key if pat.kind == KIND_MODEL_CALL_ERROR else None,
                    "stall_reason": pat.key if pat.kind == KIND_TASK_STALL else None,
                    "rate": pat.rate,
                    "successes": pat.successes,
                    "n": pat.n,
                    "ci95": list(pat.ci95),
                    "threshold": threshold,
                    "min_sample": min_sample,
                },
            )
        )
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_FIX_PROPOSED,
                task_id=task.id,
                payload={
                    "pattern_id": pat.pattern_id,
                    "experiment_id": experiment_id,
                    "metric_name": metric_name,
                    "target": target,
                    "comparator": "<=",
                    "fix_kinds": list(FIX_KINDS),
                    "proposal_written": bool(proposal_path),
                    "auto_applied": False,  # invariant: a fix is NEVER auto-applied
                },
            )
        )

        proposals.append(
            ProposedFix(
                pattern_id=pat.pattern_id,
                kind=pat.kind,
                key=pat.key,
                rate=pat.rate,
                n=pat.n,
                ci95=pat.ci95,
                proposal_status=proposal_status,
                proposal_path=proposal_path,
                experiment_id=experiment_id,
                metric_name=metric_name,
                target=target,
            )
        )

    return FailureAnalysisResult(
        workstream=task.workstream,
        patterns_detected=len(patterns),
        proposals=proposals,
        threshold=threshold,
        min_sample=min_sample,
    )


# ===========================================================================
# Verify-as-experiment: evaluate a proposed fix on real POST-FIX traffic
# ===========================================================================


def observe_and_evaluate_fix(
    conn: Any,
    experiment_id: Any,
    *,
    sink: EventSink,
    workstream: str,
    since_seq: Optional[int] = None,
    failure_report: Callable[..., dict] = _failure_report,
    record_observation: Callable[..., Any] = _record_observation,
    evaluate_experiment: Callable[..., Any] = _evaluate_experiment,
    get_experiment: Callable[..., Any] = _get_experiment,
) -> Experiment:
    """Evaluate a proposed fix experiment against REAL post-fix traffic.

    The verify hook the stakeholder asked for: after a human applies the fix and
    starts the experiment, this reads the pattern's CURRENT rate from
    :func:`runtime.quality.failure_report` scoped to the POST-FIX window
    (``since_seq`` — the monotonic events cursor captured when the fix was applied),
    records it as the experiment's observation, then runs the evidence-based
    :func:`runtime.experiment.evaluate_experiment`. A dropped rate (``<= target``) →
    ``kept``/``scaled`` (fix effective); an unchanged rate or no post-fix traffic →
    ``killed`` (ineffective / no evidence). The verdict is computed from the event
    log, never a claim.
    """
    exp = get_experiment(conn, experiment_id)
    if exp is None:
        raise ValueError(f"experiment {experiment_id} not found")
    report = failure_report(conn, workstream, since_seq=since_seq)
    rate = pattern_rate_from_report(report, exp.success_metric.name)
    if rate is not None:
        record_observation(conn, experiment_id, rate, sink=sink, workstream=workstream)
    return evaluate_experiment(conn, experiment_id, sink=sink)
