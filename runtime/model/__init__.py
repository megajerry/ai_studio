"""Model registry + router + provider abstraction + the single call wrapper (M3b).

ADR-0005 (registry/router, rules-as-data) + ADR-0012 (centralized instrumentation:
all model calls go through :func:`call_model`, which is the single place tokens +
cost are recorded). The registry is provider-agnostic; API keys are read only
inside the provider adapters (ADR-0011). Runs fully keyless via the dry-run
provider until a real key is present.
"""

from __future__ import annotations

from .call import EVENT_MODEL_CALL, call_model, select_provider
from .providers import (
    ADAPTERS,
    Completion,
    DryRunProvider,
    Message,
    Provider,
    get_adapter,
)
from .registry import (
    DEFAULT_CACHE_READ_MULTIPLIER,
    ModelSpec,
    Registry,
    RoutingPolicy,
    Tier,
    Usage,
    cost_usd,
    load_registry,
    resolve_registry_path,
)
from .router import (
    EVENT_MODEL_ROUTED,
    OverBudget,
    RouteDecision,
    route,
    route_decision,
)

__all__ = [
    # registry + cost
    "DEFAULT_CACHE_READ_MULTIPLIER",
    "ModelSpec",
    "Registry",
    "RoutingPolicy",
    "Tier",
    "Usage",
    "cost_usd",
    "load_registry",
    "resolve_registry_path",
    # router
    "EVENT_MODEL_ROUTED",
    "OverBudget",
    "RouteDecision",
    "route",
    "route_decision",
    # providers
    "ADAPTERS",
    "Completion",
    "DryRunProvider",
    "Message",
    "Provider",
    "get_adapter",
    # the single instrumented call site
    "EVENT_MODEL_CALL",
    "call_model",
    "select_provider",
]
