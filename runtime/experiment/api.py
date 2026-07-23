"""Data-access + lifecycle for the EXPERIMENT primitive (ADR-0016).

This is the studio brain's control loop over one bet:

    propose_experiment(...)          # proposed — a hypothesis + metric + budget
      → start_experiment(...)        # running  — enqueue work items toward it
        → record_observation(...)*   # evidence — work items report metric facts
          → evaluate_experiment(...) # evaluated → kept | scaled | killed

The verdict is **evidence-based**: :func:`evaluate_experiment` reads the metric
from telemetry facts (``task_cost`` for spend; ``experiment.observation`` events
for reported metrics) and applies the pure kill/scale rule in
:mod:`runtime.experiment.models` — never a model's self-report. A ``scaled``
verdict (metric strongly met within budget) raises a 🛑 approval for added budget
via :func:`runtime.approvals.request_approval` (ADR-0006). Over budget → ``killed``.

Every function takes an open ``conn`` (the caller owns the transaction boundary,
matching :mod:`runtime.tasks`/:mod:`runtime.approvals`) and an ``EventSink`` for
observability. Emitted ``experiment.*`` events carry ids / metric identity /
spend / decision only — never the hypothesis text or any argument values
(CLAUDE.md invariant 5; architecture §9 "no secret text").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, Optional
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from ..approvals import request_approval
from ..event_types import EVENT_EVALUATED, EVENT_OBSERVED, EVENT_PROPOSED, EVENT_STARTED
from ..models import make_event
from ..tasks import enqueue_task, task_cost
from .models import (
    DEFAULT_SCALE_FACTOR,
    AGGREGATES,
    Evaluation,
    Experiment,
    ExperimentDecision,
    ExperimentStatus,
    SuccessMetric,
    assert_transition,
    decide_outcome,
)

if TYPE_CHECKING:  # avoid importing psycopg-heavy modules at runtime just for types
    from ..enforce import EventSink

# The ``experiment.*`` event types are imported from the canonical
# :mod:`runtime.event_types` and re-exported from :mod:`runtime.experiment`.

#: Success-metric names read straight from spend telemetry (fully evidence-based,
#: no observation needed): maps a metric name → which measured spend it reads.
_COST_METRICS: dict[str, str] = {
    "cost_usd": "usd",
    "spent_usd": "usd",
    "total_tokens": "tokens",
    "spent_tokens": "tokens",
    "tokens": "tokens",
}

_COLUMNS = (
    "id, workstream, hypothesis, success_metric, budget_tokens, budget_usd, "
    "status, decision, observed_value, spent_tokens, spent_usd, "
    "created_at, started_at, evaluated_at"
)


# --- Row hydration ----------------------------------------------------------


def _row_to_experiment(row: dict) -> Experiment:
    """Hydrate a DB row into an :class:`Experiment` (jsonb metric, numeric→float)."""
    data = dict(row)
    data["success_metric"] = SuccessMetric.model_validate(data["success_metric"])
    if data.get("budget_usd") is not None:
        data["budget_usd"] = float(data["budget_usd"])
    if data.get("spent_usd") is not None:
        data["spent_usd"] = float(data["spent_usd"])
    if data.get("observed_value") is not None:
        data["observed_value"] = float(data["observed_value"])
    return Experiment.model_validate(data)


def _emit(sink: "EventSink", *, type: str, exp: Experiment, workstream: str, **extra: Any) -> None:
    """Emit an ``experiment.*`` event carrying identity/metric/decision — NO secret text.

    Deliberately omits ``exp.hypothesis`` (free text) and any tool/argument
    values; only the metric's identity + numeric facts + verdict travel on the wire.
    """
    payload: dict[str, Any] = {
        "experiment_id": str(exp.id),
        "workstream": exp.workstream,
        "status": exp.status.value,
        "metric_name": exp.success_metric.name,
        "target": exp.success_metric.target,
        "comparator": exp.success_metric.comparator,
        "budget_tokens": exp.budget_tokens,
        "budget_usd": exp.budget_usd,
    }
    payload.update(extra)
    sink.emit(make_event(workstream=workstream, type=type, payload=payload))


def get_experiment(conn: psycopg.Connection, experiment_id: UUID) -> Optional[Experiment]:
    """Fetch one experiment by id, or ``None`` if absent."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM experiments WHERE id = %s", (experiment_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return _row_to_experiment(row) if row else None


def list_experiments(
    conn: psycopg.Connection,
    *,
    workstream: Optional[str] = None,
    status: Optional[ExperimentStatus] = None,
) -> list[Experiment]:
    """List experiments, optionally filtered by workstream and/or status.

    Hits the ``(workstream, status)`` index — the primary read path for "a
    workstream's live/terminal bets".
    """
    clauses: list[str] = []
    params: list[object] = []
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    if status is not None:
        clauses.append("status = %s")
        params.append(status.value if isinstance(status, ExperimentStatus) else str(status))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM experiments {where} ORDER BY created_at ASC", params)
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [_row_to_experiment(r) for r in rows]


# --- propose ----------------------------------------------------------------


def propose_experiment(
    conn: psycopg.Connection,
    *,
    workstream: str,
    hypothesis: str,
    metric: SuccessMetric,
    budget_tokens: Optional[int] = None,
    budget_usd: Optional[float] = None,
    sink: "EventSink",
) -> Experiment:
    """Create a ``proposed`` experiment and emit ``experiment.proposed``.

    The metric spec is validated (comparator/aggregate allowlists) before insert,
    so an unknown comparator fails here rather than at evaluation time.
    """
    metric = metric.validate_spec()
    if not hypothesis.strip():
        raise ValueError("hypothesis must be non-empty")

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO experiments
                    (workstream, hypothesis, success_metric, budget_tokens, budget_usd, status)
                VALUES (%s, %s, %s, %s, %s, 'proposed')
                RETURNING {_COLUMNS}
                """,
                (
                    workstream,
                    hypothesis,
                    Jsonb(metric.model_dump()),
                    budget_tokens,
                    budget_usd,
                ),
            )
            exp = _row_to_experiment(cur.fetchone())
        _emit(sink, type=EVENT_PROPOSED, exp=exp, workstream=workstream)
    return exp


# --- start ------------------------------------------------------------------


def start_experiment(
    conn: psycopg.Connection,
    experiment_id: UUID,
    *,
    sink: "EventSink",
    work_items: Optional[Iterable[dict]] = None,
) -> Experiment:
    """Move a ``proposed`` experiment to ``running`` and enqueue work toward it.

    Each item in ``work_items`` (``{type, payload?, priority?, budget_tokens?}``)
    is enqueued via :func:`runtime.tasks.enqueue_task` with ``experiment_id``
    stamped into its payload, so the work is linkable back to the bet — that link
    is what :func:`evaluate_experiment` follows to read spend from ``task_cost``.
    Emits ``experiment.started`` with the created task ids.
    """
    exp = get_experiment(conn, experiment_id)
    if exp is None:
        raise ValueError(f"experiment {experiment_id} not found")
    assert_transition(exp.status, ExperimentStatus.RUNNING)  # illegal → error

    task_ids: list[str] = []
    for item in work_items or []:
        payload = dict(item.get("payload") or {})
        payload["experiment_id"] = str(exp.id)
        task = enqueue_task(
            conn,
            workstream=exp.workstream,
            type=item["type"],
            payload=payload,
            priority=int(item.get("priority", 0)),
            budget_tokens=item.get("budget_tokens"),
        )
        task_ids.append(str(task.id))

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE experiments SET status = 'running', started_at = now()
                WHERE id = %s AND status = 'proposed'
                RETURNING """
                + _COLUMNS,
                (experiment_id,),
            )
            row = cur.fetchone()
            if row is None:  # lost a race — status changed under us
                raise ValueError(f"experiment {experiment_id} is no longer 'proposed'")
            exp = _row_to_experiment(row)
        _emit(sink, type=EVENT_STARTED, exp=exp, workstream=exp.workstream, task_ids=task_ids)
    return exp


# --- evidence recording -----------------------------------------------------


def record_observation(
    conn: psycopg.Connection,
    experiment_id: UUID,
    value: float,
    *,
    sink: "EventSink",
    workstream: Optional[str] = None,
) -> None:
    """Record one measured metric datapoint as an ``experiment.observation`` event.

    This is how a work item reports evidence toward the hypothesis. Evaluation
    aggregates these facts (per the metric's ``aggregate``) — so the verdict is
    computed from the telemetry, never from a model claim. Carries only the
    experiment id + numeric value.
    """
    ws = workstream
    if ws is None:
        exp = get_experiment(conn, experiment_id)
        if exp is None:
            raise ValueError(f"experiment {experiment_id} not found")
        ws = exp.workstream
    sink.emit(
        make_event(
            workstream=ws,
            type=EVENT_OBSERVED,
            payload={"experiment_id": str(experiment_id), "value": float(value)},
        )
    )


# --- evidence reading (telemetry facts) -------------------------------------


def _measure_spend(conn: psycopg.Connection, experiment_id: UUID) -> tuple[int, float]:
    """Sum spend across every task tagged with this experiment (via ``task_cost``).

    The link is ``tasks.payload->>'experiment_id'`` (stamped by
    :func:`start_experiment`); ``task_cost`` aggregates each task's ``model.call``
    telemetry. Returns ``(spent_tokens, spent_usd)`` — zero if no tagged tasks.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM tasks WHERE payload->>'experiment_id' = %s",
            (str(experiment_id),),
        )
        task_ids = [r["id"] for r in cur.fetchall()]
    if not conn.autocommit:
        conn.commit()
    spent_tokens = 0
    spent_usd = 0.0
    for tid in task_ids:
        cost = task_cost(conn, tid)
        spent_tokens += int(cost["total_tokens"])
        spent_usd += float(cost["cost_usd"])
    return spent_tokens, spent_usd


def _read_observations(conn: psycopg.Connection, experiment_id: UUID) -> list[float]:
    """Return the recorded ``experiment.observation`` values in append order."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT (payload->>'value')::double precision AS value
            FROM events
            WHERE type = %s AND payload->>'experiment_id' = %s
            ORDER BY seq ASC
            """,
            (EVENT_OBSERVED, str(experiment_id)),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [float(r["value"]) for r in rows if r["value"] is not None]


def _observed_value(
    conn: psycopg.Connection, exp: Experiment, spent_tokens: int, spent_usd: float
) -> Optional[float]:
    """Resolve the metric's observed value from evidence.

    A cost metric (``cost_usd`` / ``*_tokens``) reads straight from measured
    spend; any other metric aggregates the ``experiment.observation`` series with
    the metric's ``aggregate``. ``None`` means no evidence → treated as a miss.
    """
    cost_kind = _COST_METRICS.get(exp.success_metric.name)
    if cost_kind is not None:
        return spent_usd if cost_kind == "usd" else float(spent_tokens)
    values = _read_observations(conn, exp.id)
    if not values:
        return None
    return AGGREGATES[exp.success_metric.aggregate](values)


# --- evaluate (the kill/scale step) -----------------------------------------


def evaluate_experiment(
    conn: psycopg.Connection,
    experiment_id: UUID,
    *,
    sink: "EventSink",
    scale_factor: float = DEFAULT_SCALE_FACTOR,
    role: str = "experiment",
) -> Experiment:
    """Read the evidence, apply the kill/scale rule, and record the verdict.

    Reads spend (``task_cost`` over tagged tasks) + the observed metric, then
    :func:`runtime.experiment.models.decide_outcome` yields ``kept`` / ``scaled``
    / ``killed`` from those **facts**. The status walks ``running → evaluated →
    <decision>`` (guarded). A ``scaled`` verdict opens a 🛑 approval for added
    budget (ADR-0006). Emits ``experiment.evaluated`` with the metric + spend +
    decision (no secret text). Returns the terminal experiment.
    """
    exp = get_experiment(conn, experiment_id)
    if exp is None:
        raise ValueError(f"experiment {experiment_id} not found")
    # Guard early with a clear error (also re-checked in the UPDATE below).
    assert_transition(exp.status, ExperimentStatus.EVALUATED)

    # 1. Gather evidence OUTSIDE any transaction (task_cost commits on its own).
    spent_tokens, spent_usd = _measure_spend(conn, experiment_id)
    observed = _observed_value(conn, exp, spent_tokens, spent_usd)

    # 2. Pure, evidence-based verdict.
    result: Evaluation = decide_outcome(
        exp.success_metric,
        observed,
        spent_tokens=spent_tokens,
        spent_usd=spent_usd,
        budget_tokens=exp.budget_tokens,
        budget_usd=exp.budget_usd,
        scale_factor=scale_factor,
    )
    decision = result.decision
    to_status = decision.to_status()

    # 3. Persist the walk running → evaluated → <decision> atomically.
    assert_transition(ExperimentStatus.EVALUATED, to_status)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE experiments
                SET status = 'evaluated', evaluated_at = now(),
                    observed_value = %s, spent_tokens = %s, spent_usd = %s
                WHERE id = %s AND status = 'running'
                RETURNING id
                """,
                (observed, spent_tokens, spent_usd, experiment_id),
            )
            if cur.fetchone() is None:
                raise ValueError(f"experiment {experiment_id} is no longer 'running'")
            cur.execute(
                f"""
                UPDATE experiments SET status = %s, decision = %s
                WHERE id = %s AND status = 'evaluated'
                RETURNING {_COLUMNS}
                """,
                (to_status.value, decision.value, experiment_id),
            )
            exp = _row_to_experiment(cur.fetchone())

    # 4. A scale verdict is a 🛑 request for more budget (blocks; batched digest).
    approval_id: Optional[str] = None
    if decision is ExperimentDecision.SCALED:
        approval = request_approval(
            conn,
            task_id=None,
            role=role,
            tool="experiment.scale",
            capabilities=["budget.increase"],
            tier="red",
            reason=(
                f"scale experiment {exp.id} ({exp.workstream}): "
                f"{exp.success_metric.name} strongly met within budget — propose more budget"
            ),
            sink=sink,
            workstream=exp.workstream,
        )
        approval_id = str(approval.id)

    # 5. Announce the verdict (facts only; no hypothesis / arg text).
    _emit(
        sink,
        type=EVENT_EVALUATED,
        exp=exp,
        workstream=exp.workstream,
        decision=decision.value,
        observed_value=observed,
        spent_tokens=spent_tokens,
        spent_usd=spent_usd,
        over_budget=result.over_budget,
        reason=result.reason,
        scale_approval_id=approval_id,
    )
    return exp
