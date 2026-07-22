"""Typed models + pure decision logic for the EXPERIMENT primitive (ADR-0016).

The experiment is the venture-studio brain's first object: *define a hypothesis,
run bounded work toward it, evaluate a metric against a target within a budget,
then kill or scale on the evidence.* Everything in this module is DB-free and
unit-testable — the pydantic row models, the guarded status machine (mirroring
:mod:`runtime.task_state`), and the pure kill/scale decision rule. The data-access
layer that persists these and reads the evidence lives in :mod:`runtime.experiment.api`;
the schema is in ``runtime/migrations/0009_experiments.sql``.

Status lifecycle (guarded; illegal transition → :class:`IllegalTransition`)::

    proposed → running → evaluated → (kept | scaled | killed)
        │          │
        └──────────┴──► killed        (abandon before / during a run)

``kept`` / ``scaled`` / ``killed`` are terminal and equal the recorded
:class:`ExperimentDecision`. The decision is computed from telemetry **facts**
(observed metric + spend vs budget), never a model claim.
"""

from __future__ import annotations

import operator
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import BaseModel


# --- Enumerations -----------------------------------------------------------


class ExperimentStatus(str, Enum):
    """Lifecycle states of an experiment (ADR-0016).

    Legal transitions live in :data:`TRANSITIONS`; every change goes through
    :func:`assert_transition`. ``KEPT`` / ``SCALED`` / ``KILLED`` are terminal and
    mirror the :class:`ExperimentDecision` reached at evaluation.
    """

    PROPOSED = "proposed"
    RUNNING = "running"
    EVALUATED = "evaluated"
    KEPT = "kept"
    SCALED = "scaled"
    KILLED = "killed"


class ExperimentDecision(str, Enum):
    """The evidence-based verdict from :func:`decide_outcome`.

    - ``KEPT``   — metric met within budget → keep as-is.
    - ``SCALED`` — metric *strongly* met within budget → propose more (🛑 budget).
    - ``KILLED`` — metric missed, or spend exceeded budget → stop.
    """

    KEPT = "kept"
    SCALED = "scaled"
    KILLED = "killed"

    def to_status(self) -> "ExperimentStatus":
        """The terminal :class:`ExperimentStatus` this decision resolves to."""
        return ExperimentStatus(self.value)


# --- Comparators + aggregates (pure) ----------------------------------------

#: Supported metric comparators — a small allowlist so a metric's comparator is
#: validated, never ``eval``'d.
COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}

#: Comparators where a *larger* observed value is better — used to define what
#: "strongly met" means (exceeding the target by the scale margin).
_HIGHER_IS_BETTER = frozenset({">=", ">"})
_LOWER_IS_BETTER = frozenset({"<=", "<"})

#: Aggregations that fold a series of observed metric values (evidence) into one.
AGGREGATES: dict[str, Callable[[list[float]], float]] = {
    "last": lambda xs: xs[-1],
    "first": lambda xs: xs[0],
    "sum": lambda xs: float(sum(xs)),
    "max": lambda xs: float(max(xs)),
    "min": lambda xs: float(min(xs)),
    "mean": lambda xs: float(sum(xs)) / len(xs),
}


def metric_meets(value: float, target: float, comparator: str) -> bool:
    """True if ``value <comparator> target`` holds. Rejects unknown comparators."""
    op = COMPARATORS.get(comparator)
    if op is None:
        raise ValueError(f"unknown comparator {comparator!r} (allowed: {sorted(COMPARATORS)})")
    return op(value, target)


def is_strongly_met(
    value: float, target: float, comparator: str, scale_factor: float
) -> bool:
    """True if the metric is met with margin — the signal to *scale*, not just keep.

    ``scale_factor`` (>= 1) is how far past the target counts as "strong": for a
    higher-is-better metric, ``value >= target * scale_factor``; for a
    lower-is-better metric, ``value <= target / scale_factor`` (well under the
    ceiling). An ``==`` metric is never "strongly" met (only met or missed).
    """
    if scale_factor < 1:
        raise ValueError("scale_factor must be >= 1")
    if not metric_meets(value, target, comparator):
        return False
    if comparator in _HIGHER_IS_BETTER:
        return value >= target * scale_factor
    if comparator in _LOWER_IS_BETTER:
        if target <= 0:  # a non-positive ceiling has no multiplicative margin
            return value <= target
        return value <= target / scale_factor
    return False  # "==" — exactly-met is kept, never scaled


# --- Row models -------------------------------------------------------------


class SuccessMetric(BaseModel):
    """What "worked" means for an experiment: a target the observed metric must hit.

    ``name`` identifies the evidence series (a telemetry metric like ``cost_usd``
    or a reported observation like ``signup_rate``); ``comparator`` is validated
    against :data:`COMPARATORS`; ``aggregate`` folds a series of observations into
    one value (:data:`AGGREGATES`).
    """

    name: str
    target: float
    comparator: str = ">="
    aggregate: str = "last"

    def validate_spec(self) -> "SuccessMetric":
        if self.comparator not in COMPARATORS:
            raise ValueError(
                f"unknown comparator {self.comparator!r} (allowed: {sorted(COMPARATORS)})"
            )
        if self.aggregate not in AGGREGATES:
            raise ValueError(
                f"unknown aggregate {self.aggregate!r} (allowed: {sorted(AGGREGATES)})"
            )
        return self

    def meets(self, value: float) -> bool:
        return metric_meets(value, self.target, self.comparator)

    def strongly_meets(self, value: float, scale_factor: float) -> bool:
        return is_strongly_met(value, self.target, self.comparator, scale_factor)


class Experiment(BaseModel):
    """A persisted experiment row — the studio's unit of bet."""

    id: UUID
    workstream: str
    hypothesis: str
    success_metric: SuccessMetric
    budget_tokens: Optional[int] = None
    budget_usd: Optional[float] = None
    status: ExperimentStatus = ExperimentStatus.PROPOSED
    decision: Optional[ExperimentDecision] = None
    #: The metric value observed at evaluation (evidence) and the spend it was
    #: judged against — persisted so the verdict is auditable/replayable.
    observed_value: Optional[float] = None
    spent_tokens: int = 0
    spent_usd: float = 0.0
    created_at: datetime
    started_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None


class Evaluation(BaseModel):
    """The pure result of the kill/scale rule — the facts behind one verdict."""

    decision: ExperimentDecision
    reason: str
    observed_value: Optional[float]
    over_budget: bool
    spent_tokens: int
    spent_usd: float


# --- Guarded status machine (mirrors runtime.task_state) ---------------------

#: Terminal states — no outgoing transitions.
TERMINAL: frozenset[str] = frozenset(
    {ExperimentStatus.KEPT.value, ExperimentStatus.SCALED.value, ExperimentStatus.KILLED.value}
)

#: The legal lifecycle. Anything not listed is rejected by :func:`assert_transition`.
#: ``killed`` is reachable early (abandon a proposed/running bet); the forward path
#: is proposed → running → evaluated → decision.
TRANSITIONS: dict[str, set[str]] = {
    ExperimentStatus.PROPOSED.value: {
        ExperimentStatus.RUNNING.value,
        ExperimentStatus.KILLED.value,
    },
    ExperimentStatus.RUNNING.value: {
        ExperimentStatus.EVALUATED.value,
        ExperimentStatus.KILLED.value,
    },
    ExperimentStatus.EVALUATED.value: {
        ExperimentStatus.KEPT.value,
        ExperimentStatus.SCALED.value,
        ExperimentStatus.KILLED.value,
    },
    ExperimentStatus.KEPT.value: set(),
    ExperimentStatus.SCALED.value: set(),
    ExperimentStatus.KILLED.value: set(),
}

#: Full canonical set (string values) for CHECK constraints + validation.
STATES: frozenset[str] = frozenset(TRANSITIONS)


class IllegalTransition(ValueError):
    """Raised when a status change is not permitted by :data:`TRANSITIONS`."""


def _val(status: Any) -> str:
    return status.value if isinstance(status, ExperimentStatus) else str(status)


def can_transition(from_status: Any, to_status: Any) -> bool:
    """True if ``from_status → to_status`` is a legal experiment transition."""
    return _val(to_status) in TRANSITIONS.get(_val(from_status), set())


def assert_transition(from_status: Any, to_status: Any) -> None:
    """Raise :class:`IllegalTransition` unless ``from_status → to_status`` is legal."""
    frm, to = _val(from_status), _val(to_status)
    if frm not in TRANSITIONS:
        raise IllegalTransition(f"unknown source status {frm!r}")
    if to not in STATES:
        raise IllegalTransition(f"unknown target status {to!r}")
    if to not in TRANSITIONS[frm]:
        raise IllegalTransition(
            f"illegal transition {frm!r} → {to!r} "
            f"(allowed: {sorted(TRANSITIONS[frm]) or 'none — terminal'})"
        )


def is_terminal(status: Any) -> bool:
    """True if ``status`` is terminal (``kept`` / ``scaled`` / ``killed``)."""
    return _val(status) in TERMINAL


# --- The kill/scale decision rule (pure, evidence-based) --------------------

#: Default margin past the target that counts as "strongly met" → scale.
DEFAULT_SCALE_FACTOR = 1.25


def is_over_budget(
    spent_tokens: int,
    spent_usd: float,
    budget_tokens: Optional[int],
    budget_usd: Optional[float],
) -> bool:
    """True if measured spend exceeded either declared budget ceiling."""
    if budget_tokens is not None and spent_tokens > budget_tokens:
        return True
    if budget_usd is not None and spent_usd > budget_usd:
        return True
    return False


def decide_outcome(
    metric: SuccessMetric,
    observed_value: Optional[float],
    *,
    spent_tokens: int,
    spent_usd: float,
    budget_tokens: Optional[int],
    budget_usd: Optional[float],
    scale_factor: float = DEFAULT_SCALE_FACTOR,
) -> Evaluation:
    """Compute the kill/scale verdict from facts — the moat's core rule.

    Priority (budget is a hard gate, then the metric):

    1. **Over budget** → ``KILLED``. A bet that blew its budget is stopped even if
       the metric looks good — spend is a fact, not a claim.
    2. **Metric missed / no evidence** → ``KILLED``.
    3. **Metric strongly met** (:func:`is_strongly_met`) → ``SCALED`` — worth more.
    4. **Metric met** → ``KEPT``.

    ``observed_value is None`` means no evidence was recorded — treated as a miss
    (a bet with no signal is killed, never kept on faith).
    """
    over = is_over_budget(spent_tokens, spent_usd, budget_tokens, budget_usd)
    if over:
        return Evaluation(
            decision=ExperimentDecision.KILLED,
            reason="over budget: measured spend exceeded the declared ceiling",
            observed_value=observed_value,
            over_budget=True,
            spent_tokens=spent_tokens,
            spent_usd=spent_usd,
        )
    if observed_value is None:
        return Evaluation(
            decision=ExperimentDecision.KILLED,
            reason="no evidence: no metric observations recorded",
            observed_value=None,
            over_budget=False,
            spent_tokens=spent_tokens,
            spent_usd=spent_usd,
        )
    if not metric.meets(observed_value):
        return Evaluation(
            decision=ExperimentDecision.KILLED,
            reason=(
                f"metric missed: {metric.name}={observed_value} "
                f"not {metric.comparator} {metric.target}"
            ),
            observed_value=observed_value,
            over_budget=False,
            spent_tokens=spent_tokens,
            spent_usd=spent_usd,
        )
    if metric.strongly_meets(observed_value, scale_factor):
        return Evaluation(
            decision=ExperimentDecision.SCALED,
            reason=(
                f"metric strongly met: {metric.name}={observed_value} "
                f"beyond {metric.comparator} {metric.target} within budget"
            ),
            observed_value=observed_value,
            over_budget=False,
            spent_tokens=spent_tokens,
            spent_usd=spent_usd,
        )
    return Evaluation(
        decision=ExperimentDecision.KEPT,
        reason=(
            f"metric met: {metric.name}={observed_value} "
            f"{metric.comparator} {metric.target} within budget"
        ),
        observed_value=observed_value,
        over_budget=False,
        spent_tokens=spent_tokens,
        spent_usd=spent_usd,
    )
