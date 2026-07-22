"""Router selection, fallback chain, over-budget downshift, and event emission."""

from __future__ import annotations

import pytest

from runtime.enforce import MemoryEventSink
from runtime.model.registry import Registry, Tier
from runtime.model.router import (
    EVENT_MODEL_ROUTED,
    OverBudget,
    route,
    route_decision,
)
from runtime.policy import BudgetContext


def _registry():
    from runtime.model.registry import load_registry

    return load_registry()


@pytest.mark.parametrize(
    "task_type,quality,expected",
    [
        ("plan", "high", "claude-opus-4.8"),
        ("plan", "low", "claude-sonnet-5"),
        ("code", "standard", "claude-sonnet-5"),
        ("classify", "standard", "claude-haiku-4.5"),
        ("classify", "high", "claude-sonnet-5"),
        ("embed", "standard", "text-embedding-005"),
        ("unmapped-type", "high", "claude-opus-4.8"),  # falls to default
    ],
)
def test_selection_by_task_type_and_quality(task_type, quality, expected):
    d = route_decision(task_type, quality, registry=_registry())
    assert d.model.id == expected


def test_selection_is_deterministic():
    reg = _registry()
    a = route_decision("execute", "standard", registry=reg)
    b = route_decision("execute", "standard", registry=reg)
    assert a.model.id == b.model.id


def test_fallback_chain_skips_missing_model():
    # A PM tier whose first choice is absent from the catalog -> second wins.
    reg = Registry.from_dict(
        {
            "models": [
                {"id": "gemini-3.1-pro", "provider": "google", "tier": "pm",
                 "price_in": 2, "price_out": 12, "context_window": 1000000},
            ],
            "routing": {
                "default": {"high": "pm"},
                "tiers": {"pm": ["claude-opus-4.8", "gemini-3.1-pro"]},
            },
        }
    )
    d = route_decision("anything", "high", registry=reg)
    assert d.model.id == "gemini-3.1-pro"  # opus absent -> fallback


def test_over_budget_downshifts_to_cheaper_tier():
    reg = _registry()
    # pm would be chosen, but budget is blown -> downshift to mid.
    budget = BudgetContext(spent_tokens=1000, budget_tokens=1000, estimated_tokens=1)
    d = route_decision("plan", "high", registry=reg, budget_ctx=budget)
    assert d.downshifted is True
    assert d.tier is Tier.MID
    assert "over budget" in d.reason


def test_over_budget_raises_when_no_cheaper_tier():
    reg = _registry()
    # classify/standard -> cheap tier, which has no downshift target -> raise.
    budget = BudgetContext(spent_tokens=1000, budget_tokens=1000, estimated_tokens=1)
    with pytest.raises(OverBudget):
        route_decision("classify", "standard", registry=reg, budget_ctx=budget)


def test_within_budget_does_not_downshift():
    reg = _registry()
    budget = BudgetContext(spent_tokens=0, budget_tokens=1_000_000, estimated_tokens=10)
    d = route_decision("plan", "high", registry=reg, budget_ctx=budget)
    assert d.downshifted is False
    assert d.model.id == "claude-opus-4.8"


def test_route_emits_model_routed_event():
    reg = _registry()
    sink = MemoryEventSink()
    spec = route("plan", "high", registry=reg, sink=sink, workstream="productivity")
    assert spec.id == "claude-opus-4.8"
    assert sink.types() == [EVENT_MODEL_ROUTED]
    payload = sink.events[0].payload
    assert payload["model"] == "claude-opus-4.8"
    assert payload["provider"] == "anthropic"
    assert payload["tier"] == "pm"
    assert payload["reason"]


def test_invalid_quality_rejected():
    with pytest.raises(ValueError):
        route_decision("plan", "ultra", registry=_registry())
