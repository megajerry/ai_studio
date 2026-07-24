"""Workstream configuration + registration — a vertical is config, not code.

This package is the **config/registration** half of the workstream-bootstrap
primitive (state/backlog.md item 1): a rules-as-data record per vertical that
DRIVES the existing role/verify/budget/policy/skill/memory seams, so starting a
vertical means writing ``workstreams/<name>/config.yaml`` — never new role code.

- :class:`WorkstreamConfig` + loaders (:func:`resolve_workstream_config`) — the
  strict, secret-free config (charter/overlays/budget/policy-grants/skills/
  checkers/memory-seed/bucket).
- :func:`bootstrap_workstream` — idempotently seed the config's memory + budget.
- :data:`BUILTIN_CHECKERS` — the domain verify-checkers a config enables by name.

The cross-workstream request contract (typed ``feature_request`` + receiving-PM
intake) is a separate follow-up (state/backlog.md item 1), NOT built here.
"""

from __future__ import annotations

from .bootstrap import BootstrapResult, bootstrap_workstream
from .checkers import BUILTIN_CHECKERS, register_checker, video_audit
from .config import (
    BudgetSpec,
    CapacityStewardSpec,
    MemorySeedItem,
    SkillsSpec,
    WorkstreamConfig,
    WorkstreamConfigError,
    config_path,
    load_config_file,
    load_workstream_config,
    resolve_workstream_config,
    workstreams_dir,
)

__all__ = [
    "WorkstreamConfig",
    "WorkstreamConfigError",
    "BudgetSpec",
    "CapacityStewardSpec",
    "SkillsSpec",
    "MemorySeedItem",
    "config_path",
    "load_config_file",
    "load_workstream_config",
    "resolve_workstream_config",
    "workstreams_dir",
    "bootstrap_workstream",
    "BootstrapResult",
    "BUILTIN_CHECKERS",
    "register_checker",
    "video_audit",
]
