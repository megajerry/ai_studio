"""Model registry + routing policy — rules as DATA, not code (ADR-0005).

A versioned catalog of models (`ModelSpec`) plus the routing policy
(`RoutingPolicy`) that maps ``(task_type, quality) → tier → an ordered model
preference / fallback chain``. Both live in the *same* YAML file so a real
deployment tunes model choice and pricing by editing config, never code.

Resolution order mirrors :mod:`runtime.policy` (env → local → committed example):

    1. $AI_STUDIO_MODELS_FILE      (explicit path)
    2. runtime/models.yaml          (real catalog, git-ignored)
    3. runtime/models.example.yaml  (committed, documented default)

The example file carries NO secrets — a `ModelSpec` never holds an API key. Keys
are `.env` entries read *inside* the provider adapters only (ADR-0011); the
registry is provider-agnostic and does not care which keys are present.

Cost is derived here from tokens × registry price (ADR-0012): :func:`cost_usd`
is the single, tested cost function the instrumented call wrapper uses.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

_ENV_MODELS_PATH = "AI_STUDIO_MODELS_FILE"
_RUNTIME_DIR = Path(__file__).resolve().parent.parent
_LOCAL_MODELS = _RUNTIME_DIR / "models.yaml"
_EXAMPLE_MODELS = _RUNTIME_DIR / "models.example.yaml"

#: Default price multiplier for a cache *read* token vs. a fresh input token.
#: The cost model (docs/cost-model.md) uses ~0.1 (e.g. Opus cached input $0.50
#: vs $5). A `ModelSpec` may override it per-model.
DEFAULT_CACHE_READ_MULTIPLIER = 0.1


class Tier(str, Enum):
    """Quality/cost bands models are grouped into (docs/cost-model.md §2).

    Named for the role they serve rather than the ADR's 🟢/🟡 approval tiers
    (those gate *actions*, not model choice). ``PM`` = premium/planner,
    ``MID`` = everyday executor, ``CHEAP`` = classify/route/high-volume,
    ``EMBEDDING`` = vector embeddings (a separate axis — no generation).
    """

    PM = "pm"
    MID = "mid"
    CHEAP = "cheap"
    EMBEDDING = "embedding"


class ModelSpec(BaseModel):
    """One catalog entry: a model + its economics + provenance.

    Prices are USD per **1M tokens**, matching docs/model-shortlist.md. A spec
    holds no credentials — the ``provider`` string names which adapter (and thus
    which env key) services it, but the key itself never lives here.
    """

    id: str
    provider: str
    tier: Tier
    price_in: float  # USD / 1M input tokens
    price_out: float  # USD / 1M output tokens
    context_window: int
    #: Multiplier applied to cached-input tokens (cache reads are cheaper).
    cache_read_multiplier: float = DEFAULT_CACHE_READ_MULTIPLIER
    #: Free-form capability tags used for human/sourcing reasoning (not routing).
    task_fit: list[str] = Field(default_factory=list)
    #: Where the numbers came from + the date they were captured (ADR-0005).
    provenance: str = ""
    provenance_date: str = ""


class Usage(BaseModel):
    """Token accounting for a single model call (ADR-0012).

    ``cached_tokens`` are a subset of ``input_tokens`` billed at the cheaper
    cache-read rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def cost_usd(spec: ModelSpec, usage: Usage) -> float:
    """Cost of one call = tokens × registry price (ADR-0012).

    Fresh input tokens bill at ``price_in``; the cached subset bills at
    ``price_in × cache_read_multiplier``; output at ``price_out``. Prices are per
    1M tokens. This is the single cost function the call wrapper uses, so cost is
    always computed from the registry, never hard-coded at a call site.
    """
    cached = max(0, min(usage.cached_tokens, usage.input_tokens))
    fresh_in = usage.input_tokens - cached
    per_million = (
        fresh_in * spec.price_in
        + cached * spec.price_in * spec.cache_read_multiplier
        + usage.output_tokens * spec.price_out
    )
    return per_million / 1_000_000.0


class RoutingPolicy(BaseModel):
    """The router's rules, as data (ADR-0005).

    - ``task_types``: ``task_type → {quality → tier}`` overrides.
    - ``default``: ``quality → tier`` fallback when a task_type is unmapped.
    - ``tiers``: ``tier → ordered list of model ids`` (the fallback chain; the
      first id present in the registry wins).
    - ``downshift``: ``tier → cheaper tier`` used to route down when a call
      would exceed budget (ADR-0006/0012).
    """

    task_types: dict[str, dict[str, str]] = Field(default_factory=dict)
    default: dict[str, str] = Field(default_factory=dict)
    tiers: dict[str, list[str]] = Field(default_factory=dict)
    downshift: dict[str, str] = Field(default_factory=dict)

    def tier_for(self, task_type: str, quality: str) -> Tier:
        """Resolve ``(task_type, quality)`` to a :class:`Tier` deterministically."""
        by_type = self.task_types.get(task_type)
        name = None
        if by_type is not None:
            name = by_type.get(quality)
        if name is None:
            name = self.default.get(quality)
        if name is None:
            raise KeyError(
                f"no tier mapping for task_type={task_type!r} quality={quality!r} "
                "(and no default) — check the routing policy"
            )
        return Tier(name)

    def candidates(self, tier: Tier) -> list[str]:
        """Ordered model-id preference for a tier (the fallback chain)."""
        return list(self.tiers.get(tier.value, []))

    def cheaper_tier(self, tier: Tier) -> Optional[Tier]:
        """The tier to downshift to when over budget, or ``None`` if none."""
        name = self.downshift.get(tier.value)
        if name is None or name == tier.value:
            return None
        return Tier(name)


class Registry(BaseModel):
    """The loaded catalog + policy."""

    models: dict[str, ModelSpec]
    policy: RoutingPolicy

    def get(self, model_id: str) -> Optional[ModelSpec]:
        return self.models.get(model_id)

    def by_tier(self, tier: Tier) -> list[ModelSpec]:
        return [m for m in self.models.values() if m.tier is tier]

    @classmethod
    def from_dict(cls, data: dict) -> "Registry":
        data = data or {}
        specs_raw = data.get("models", []) or []
        models: dict[str, ModelSpec] = {}
        for raw in specs_raw:
            spec = ModelSpec.model_validate(raw)
            if spec.id in models:
                raise ValueError(f"duplicate model id in registry: {spec.id!r}")
            models[spec.id] = spec
        policy = RoutingPolicy.model_validate(data.get("routing", {}) or {})
        return cls(models=models, policy=policy)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Registry":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})


def resolve_registry_path() -> Path:
    """Active registry file per the resolution order (env → local → example)."""
    env_path = os.environ.get(_ENV_MODELS_PATH)
    if env_path:
        return Path(env_path)
    if _LOCAL_MODELS.exists():
        return _LOCAL_MODELS
    return _EXAMPLE_MODELS


def load_registry(path: str | Path | None = None) -> Registry:
    """Load the :class:`Registry`, defaulting to :func:`resolve_registry_path`."""
    return Registry.from_yaml(path if path is not None else resolve_registry_path())
