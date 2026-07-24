"""Router — deterministic model selection from a data-driven policy (ADR-0005).

Given ``(task_type, quality, budget, latency)`` from the PM, the router resolves
a :class:`~runtime.model.registry.Tier` from the routing policy, then walks that
tier's ordered fallback chain and returns the first model present in the catalog.
Selection is **pure and deterministic** — same inputs, same policy, same choice.

Every decision is logged as a ``model.routed`` event (the chosen model + the
reason), via the injected :class:`~runtime.enforce.EventSink`, so routing is
replayable (ADR-0005/0012).

Budget: if a token :class:`~runtime.policy.BudgetContext` is supplied and the
call would exceed the cap, the router routes DOWN to the policy's cheaper tier
(and says so in the reason). If there is no cheaper tier to fall to, it raises
:class:`OverBudget` — over-budget is a 🛑 concern (ADR-0006), never a silent
overspend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from ..enforce import EventSink, NullEventSink
from ..event_types import EVENT_MODEL_ROUTED
from ..models import make_event
from ..policy import BudgetContext
from .registry import ModelSpec, Registry, Tier, load_registry

#: The routing-decision event type (``model.routed``) is imported from the
#: canonical :mod:`runtime.event_types`.

_VALID_QUALITY = {"high", "standard", "low"}


class OverBudget(Exception):
    """Raised when a call would exceed budget and no cheaper tier can serve it."""

    def __init__(self, tier: Tier, budget: BudgetContext) -> None:
        self.tier = tier
        self.budget = budget
        super().__init__(
            f"over budget at cheapest available tier {tier.value!r}: "
            f"{budget.spent_tokens}+{budget.estimated_tokens} > {budget.budget_tokens}"
        )


@dataclass(frozen=True)
class RouteDecision:
    """The router's full answer (returned by :func:`route_decision`)."""

    model: ModelSpec
    tier: Tier
    reason: str
    task_type: str
    quality: str
    #: True when the tier was lowered because the original would exceed budget.
    downshifted: bool = False

    def to_payload(self) -> dict:
        """JSON-serializable summary for the ``model.routed`` event."""
        return {
            "model": self.model.id,
            "provider": self.model.provider,
            "tier": self.tier.value,
            "task_type": self.task_type,
            "quality": self.quality,
            "downshifted": self.downshifted,
            "reason": self.reason,
        }


def _first_available(registry: Registry, tier: Tier) -> Optional[ModelSpec]:
    """First model in the tier's fallback chain that exists in the catalog.

    Falls back to any catalog model of that tier if the chain lists none that
    are present (keeps routing working even if the chain drifts from the catalog).
    """
    for model_id in registry.policy.candidates(tier):
        spec = registry.get(model_id)
        if spec is not None:
            return spec
    by_tier = registry.by_tier(tier)
    return by_tier[0] if by_tier else None


def next_candidate(
    registry: Registry, tier: Tier, after_model_id: str
) -> Optional[ModelSpec]:
    """The next model in ``tier``'s chain AFTER ``after_model_id`` (runtime fallback).

    Used by :func:`runtime.model.call.call_model` when a provider raises
    :class:`~runtime.model.providers.base.ProviderFallback` (e.g. the Cursor CLI
    times out): the routed model failed *at call time*, so we walk the same
    data-driven tier chain to the next present model — a metered fallback — and
    retry. Selection stays deterministic and rules-as-data (ADR-0005): the chain
    order in the registry decides the fallback, not code. Returns ``None`` if the
    failed model is last (or absent) in the chain — nothing left to fall to.
    """
    chain = registry.policy.candidates(tier)
    seen = False
    for model_id in chain:
        if not seen:
            if model_id == after_model_id:
                seen = True
            continue
        spec = registry.get(model_id)
        if spec is not None:
            return spec
    return None


def route_decision(
    task_type: str,
    quality: str = "standard",
    *,
    registry: Optional[Registry] = None,
    budget_ctx: Optional[BudgetContext] = None,
    latency: Optional[str] = None,
) -> RouteDecision:
    """Resolve a :class:`RouteDecision` without emitting an event (pure).

    ``latency`` is accepted for interface completeness (the PM's latency SLA) and
    reserved for future latency-aware tie-breaking; today's chains are ordered by
    quality/cost and ``latency`` does not change the deterministic pick.
    """
    if quality not in _VALID_QUALITY:
        raise ValueError(f"quality must be one of {sorted(_VALID_QUALITY)}, got {quality!r}")
    if registry is None:
        registry = load_registry()

    tier = registry.policy.tier_for(task_type, quality)
    spec = _first_available(registry, tier)
    if spec is None:
        raise LookupError(f"no model available for tier {tier.value!r}")

    reason = f"task_type={task_type!r} quality={quality!r} -> tier {tier.value!r}"

    # Budget: downshift to a cheaper tier rather than overspend; raise if none.
    downshifted = False
    if budget_ctx is not None and budget_ctx.would_exceed:
        while budget_ctx.would_exceed:
            cheaper = registry.policy.cheaper_tier(tier)
            if cheaper is None:
                raise OverBudget(tier, budget_ctx)
            cheaper_spec = _first_available(registry, cheaper)
            if cheaper_spec is None:
                raise OverBudget(tier, budget_ctx)
            tier, spec, downshifted = cheaper, cheaper_spec, True
            # Budget is token-based and model-independent, so one downshift step
            # satisfies "prefer cheaper"; keep the cheaper model and note it.
            reason = (
                f"task_type={task_type!r} quality={quality!r}: over budget "
                f"({budget_ctx.spent_tokens}+{budget_ctx.estimated_tokens} > "
                f"{budget_ctx.budget_tokens}) -> downshift to tier {tier.value!r}"
            )
            break

    return RouteDecision(
        model=spec,
        tier=tier,
        reason=reason,
        task_type=task_type,
        quality=quality,
        downshifted=downshifted,
    )


def route(
    task_type: str,
    quality: str = "standard",
    *,
    registry: Optional[Registry] = None,
    budget_ctx: Optional[BudgetContext] = None,
    latency: Optional[str] = None,
    sink: Optional[EventSink] = None,
    workstream: str = "productivity",
    task_id: Optional[UUID] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> ModelSpec:
    """Select a model and emit a ``model.routed`` event (ADR-0005).

    Thin wrapper over :func:`route_decision` that logs the decision. Returns the
    chosen :class:`ModelSpec`. Raises :class:`OverBudget` if the call cannot be
    served within budget at any tier.
    """
    decision = route_decision(
        task_type,
        quality,
        registry=registry,
        budget_ctx=budget_ctx,
        latency=latency,
    )
    (sink or NullEventSink()).emit(
        make_event(
            workstream=workstream,
            type=EVENT_MODEL_ROUTED,
            task_id=task_id,
            trace_id=trace_id,
            span_id=span_id,
            payload=decision.to_payload(),
        )
    )
    return decision.model
