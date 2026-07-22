"""Search config — registry-as-data (ADR-0005), resolved like the model registry.

Which provider serves searches and how long results are cached is *data*, not
code, so swapping Tavily → Exa → Brave (or tuning the TTL) is a config edit. The
resolution order mirrors :mod:`runtime.policy` / the model registry
(env → local → committed example):

    1. $AI_STUDIO_SEARCH_FILE     (explicit path)
    2. runtime/search.yaml         (real config, git-ignored)
    3. runtime/search.example.yaml (committed, documented default)

The example file carries NO secrets — provider names only. API keys are ``.env``
entries read *inside* the provider adapters (ADR-0011); config never holds one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

_ENV_SEARCH_PATH = "AI_STUDIO_SEARCH_FILE"
_RUNTIME_DIR = Path(__file__).resolve().parent.parent
_LOCAL_SEARCH = _RUNTIME_DIR / "search.yaml"
_EXAMPLE_SEARCH = _RUNTIME_DIR / "search.example.yaml"

#: Fallback default TTL (seconds) when the config omits one.
DEFAULT_TTL_S = 3600


class SearchConfig(BaseModel):
    """Search gateway config as data."""

    #: Which provider the gateway uses when the caller does not name one. Matches
    #: a provider ``name`` (``dryrun`` | ``tavily`` | ``exa`` | ``brave``).
    default_provider: str = "dryrun"
    #: Cache TTL in seconds. ``None`` means cached results never expire.
    ttl_s: Optional[int] = DEFAULT_TTL_S
    #: When true, the gateway also remembers a compact result summary into the
    #: Knowledge memory layer for reuse (architecture §9 "→ Memory"). Off by
    #: default — opt-in, never on the hot path unless explicitly enabled.
    remember_results: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "SearchConfig":
        return cls.model_validate(data or {})

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SearchConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})


def resolve_search_path() -> Path:
    """Active search config file per the resolution order (env → local → example)."""
    env_path = os.environ.get(_ENV_SEARCH_PATH)
    if env_path:
        return Path(env_path)
    if _LOCAL_SEARCH.exists():
        return _LOCAL_SEARCH
    return _EXAMPLE_SEARCH


def load_search_config(path: str | Path | None = None) -> SearchConfig:
    """Load a :class:`SearchConfig`, defaulting to :func:`resolve_search_path`."""
    return SearchConfig.from_yaml(path if path is not None else resolve_search_path())


__all__ = [
    "DEFAULT_TTL_S",
    "SearchConfig",
    "load_search_config",
    "resolve_search_path",
]
