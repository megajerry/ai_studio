"""WorkstreamConfig — a vertical is config, not code (ADR-0002, ADR-0018).

A **vertical** (a video channel, a game, a product) is instantiated on top of
this horizontal platform (ADR-0002). Historically starting one would have meant
*writing* PM/Executor/Verifier subclasses. The role-customization seams removed
that need — :func:`runtime.roles.prompt.compose_role_prompt` takes a workstream
charter + per-role overlay, and :class:`runtime.roles.checkers.CheckerRegistry`
takes a domain verify-check — so all that remains is a **record** that supplies
those knobs. This module is that record: a rules-as-data config per workstream
that DRIVES the existing seams. No workstream needs new role code.

The config is **data, never code, never secrets** (CLAUDE.md invariants 5 & 6,
ADR-0011). It lives in the platform repo (this repo) under
``workstreams/<name>/config.yaml`` — the *definition* half of the vertical
(ADR-0018: state→DB, artifacts→object store, product→own repo, definition→here).
Keys/credentials are provisioned separately (onboarding → git-ignored env); this
file only NAMES a bucket / references skills, never embeds a secret.

Fields (all optional except ``name``):

- ``name`` — the workstream slug (must match its directory name).
- ``charter`` / ``objective`` — the vertical's mission + operating context,
  layered into every role prompt as the ``### Workstream charter`` section.
- ``role_overlays`` — per-role prompt fragments (``pm`` / ``executor`` /
  ``verifier`` / …), each layered as that role's ``### Role overlay`` section.
- ``budget`` — a ``cap_usd`` / ``cap_tokens`` / ``period`` ceiling wired into
  :mod:`runtime.budget` (seeded by :func:`runtime.workstream.bootstrap`).
- ``capacity_steward`` — opt-in (``enabled: true``) for the OPTIONAL dedicated
  Capacity Steward role (ADR-0022 C2). OFF by default (PM is the accountable
  steward); enabling it activates a monitor that FLAGS budget breaches early +
  RECOMMENDS actions (:func:`runtime.roles.capacity_steward.capacity_steward_enabled`).
- ``policy_grants`` — role→capabilities ADDED for THIS workstream, **unioned onto**
  the base policy (:meth:`effective_policy`) so a vertical can grant e.g. its
  ``operator`` a publish 🔴 tool without editing the shared policy file — and
  WITHOUT silently dropping the role's base capabilities (additive, never REPLACE).
- ``policy_revocations`` — role→capabilities REMOVED for THIS workstream, the
  EXPLICIT way to scope a role DOWN (least privilege) after the union. Removal is
  deliberate here, never a silent side effect of adding a grant. Applied after the
  union, so a cap in both is removed (revocation wins).
- ``skills`` — the skill ``names`` (a subset of the shared corpus) and/or a
  workstream-local ``dir`` this vertical's roles draw on.
- ``checkers`` — which built-in domain verify-checkers to register on top of the
  horizontal ``marker`` gate (:meth:`checker_registry`).
- ``memory_seed`` — initial Knowledge lessons/docs seeded (idempotently) into the
  workstream's memory on bootstrap.
- ``object_store_bucket`` — the workstream's artifact bucket NAME (ADR-0018);
  a name only, never credentials.

Loading is strict: an unknown field, an unknown capability/period/checker, or a
name/dir mismatch raises a clear :class:`WorkstreamConfigError` naming the file —
misconfiguration fails loudly, never silently.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from ..budget import VALID_PERIODS
from ..capabilities import Capability
from ..policy import PolicyConfig
from ..roles.checkers import CheckerRegistry, default_registry
from ..skills import SkillRegistry
from .checkers import BUILTIN_CHECKERS

_ENV_WORKSTREAMS_DIR = "AI_STUDIO_WORKSTREAMS_DIR"
#: Repo root is two parents up from this file (runtime/workstream/config.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DIR = _REPO_ROOT / "workstreams"
#: The config file name inside a workstream directory.
CONFIG_FILENAME = "config.yaml"


class WorkstreamConfigError(ValueError):
    """A workstream config is missing, malformed, or references something unknown.

    Carries the offending file path so a misconfiguration is easy to locate.
    """


class BudgetSpec(BaseModel):
    """A workstream's spend ceiling — wired into :mod:`runtime.budget`.

    ``cap_usd`` / ``cap_tokens`` may each be set or ``None`` (that resource
    unconstrained); ``period`` is the enforcement window (:data:`VALID_PERIODS`).
    """

    model_config = ConfigDict(extra="forbid")

    cap_usd: Optional[float] = None
    cap_tokens: Optional[int] = None
    period: str = "monthly"

    @field_validator("period")
    @classmethod
    def _valid_period(cls, v: str) -> str:
        if v not in VALID_PERIODS:
            raise ValueError(f"invalid budget period {v!r} (allowed: {VALID_PERIODS})")
        return v


class CapacityStewardSpec(BaseModel):
    """Opt-in config for the OPTIONAL dedicated Capacity Steward role (ADR-0022 C2).

    OFF by default: PM is the accountable steward (every role is already
    budget-aware). A vertical at scale sets ``enabled: true`` to activate a dedicated
    monitor that FLAGS projected budget breaches early + RECOMMENDS actions (reviewable
    events only — it never enforces or raises a ceiling). ``window_min`` tunes the
    burn-rate look-back; ``horizon_min`` overrides the period-end projection horizon
    (``None`` → derive it from the budget period). Config-not-code — no code change to
    turn the role on/off for a workstream.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    window_min: Optional[float] = None
    horizon_min: Optional[float] = None


class SkillsSpec(BaseModel):
    """The skills this workstream's roles draw on.

    ``names`` restricts to a subset of the discovered corpus (the roles still run
    their own relevance ``select``, but only over these). ``dir`` (relative to the
    workstream directory, or absolute) points at a workstream-local skills root.
    Both optional; with neither set the shared corpus is used unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(default_factory=list)
    dir: Optional[str] = None


class MemorySeedItem(BaseModel):
    """One initial Knowledge lesson/doc seeded into the workstream's memory.

    ``global_`` (YAML key ``global``) stores it in the shared global corpus
    (``'*'``) instead of the workstream's own — for a lesson every vertical should
    see. Optional ``kind`` tags the item (default ``lesson``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str
    global_: bool = Field(default=False, alias="global")
    kind: str = "lesson"

    @field_validator("text")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("memory_seed item text must be non-empty")
        return v


class WorkstreamConfig(BaseModel):
    """A vertical's full definition as data (see the module docstring).

    Strict: unknown top-level or nested fields raise, so a typo never silently
    no-ops. The helpers (:meth:`charter`, :meth:`overlay_for`,
    :meth:`checker_registry`, :meth:`effective_policy`, :meth:`effective_skills`)
    are how the config DRIVES the existing runtime seams.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    charter: str = ""
    objective: str = ""
    role_overlays: dict[str, str] = Field(default_factory=dict)
    budget: Optional[BudgetSpec] = None
    capacity_steward: Optional[CapacityStewardSpec] = None
    policy_grants: dict[str, list[str]] = Field(default_factory=dict)
    policy_revocations: dict[str, list[str]] = Field(default_factory=dict)
    skills: Optional[SkillsSpec] = None
    checkers: list[str] = Field(default_factory=list)
    memory_seed: list[MemorySeedItem] = Field(default_factory=list)
    object_store_bucket: Optional[str] = None

    #: The directory the config was loaded from (set by the loader), used to
    #: resolve a relative ``skills.dir``. Not a config field.
    _source_dir: Optional[Path] = PrivateAttr(default=None)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("workstream name must be non-empty")
        return v.strip()

    @model_validator(mode="after")
    def _charter_from_objective(self) -> "WorkstreamConfig":
        # ``objective`` is the short-form alias for ``charter``; if only one is
        # given, both read the same so ``cfg.charter`` is always populated.
        if not self.charter and self.objective:
            self.charter = self.objective
        elif self.charter and not self.objective:
            self.objective = self.charter
        return self

    @field_validator("policy_grants", "policy_revocations")
    @classmethod
    def _valid_capabilities(
        cls, grants: dict[str, list[str]], info
    ) -> dict[str, list[str]]:
        field = info.field_name
        for role, caps in grants.items():
            for cap in caps:
                try:
                    Capability(cap)
                except ValueError as exc:
                    valid = sorted(c.value for c in Capability)
                    raise ValueError(
                        f"{field}[{role!r}] has unknown capability {cap!r}; "
                        f"valid: {valid}"
                    ) from exc
        return grants

    @field_validator("checkers")
    @classmethod
    def _valid_checkers(cls, names: list[str]) -> list[str]:
        for name in names:
            if name not in BUILTIN_CHECKERS:
                raise ValueError(
                    f"unknown checker {name!r}; available: {sorted(BUILTIN_CHECKERS)}"
                )
        return names

    # --- seam drivers -------------------------------------------------------

    def overlay_for(self, role: str) -> Optional[str]:
        """The per-role prompt overlay for ``role`` (``None`` if none configured)."""
        return self.role_overlays.get(role)

    def checker_registry(self) -> CheckerRegistry:
        """A :class:`CheckerRegistry` = the horizontal ``marker`` gate + this
        workstream's configured domain checkers (looked up in the built-in map).

        Passed to :func:`runtime.roles.verifier.verify` as ``checkers=`` so a
        vertical's domain check (e.g. ``video_audit``) runs with no Verifier edit.
        """
        reg = default_registry()  # pre-registers "marker"
        for name in self.checkers:
            checker = BUILTIN_CHECKERS.get(name)
            if checker is None:  # pragma: no cover - guarded at validation time
                raise WorkstreamConfigError(
                    f"unknown checker {name!r}; available: {sorted(BUILTIN_CHECKERS)}"
                )
            reg.register(name, checker)
        return reg

    def effective_policy(self, base: PolicyConfig) -> PolicyConfig:
        """``base`` policy with this workstream's grants **unioned on** and its
        revocations removed.

        Composition (decided deliberately — see below):

            effective[role] = (base[role] ∪ policy_grants[role]) − policy_revocations[role]

        - ``policy_grants`` is **ADDITIVE**: a role named there KEEPS all its base
          capabilities and gains the listed ones. Adding one grant never silently
          drops the base set (that would be a least-privilege footgun — a role
          could lose a capability it needs while the config author only meant to
          add one). This is a union, **not** a REPLACE.
        - ``policy_revocations`` is the EXPLICIT, deliberate way to scope a role
          DOWN. It is applied AFTER the union, so a capability listed in both is
          removed (revocation wins). Removing a capability the role doesn't have
          is a harmless no-op.

        Roles named in neither keep their base grant. With no grants AND no
        revocations the base is returned unchanged (behavior-preserving). Tier
        overrides are always inherited from the base.

        Decision (locked by tests): earlier this method REPLACED a role's grant
        set with the workstream list, which forced a config author to re-list
        every base capability just to add one (see the old example config) and
        risked silently dropping a base capability on drift. Union-plus-explicit-
        revocation preserves both directions of least-privilege — a vertical can
        still scope a role down — while making removal deliberate, not accidental.
        """
        if not self.policy_grants and not self.policy_revocations:
            return base
        roles = dict(base.roles)
        for role, caps in self.policy_grants.items():
            additions = frozenset(Capability(c) for c in caps)
            roles[role] = roles.get(role, frozenset()) | additions
        for role, caps in self.policy_revocations.items():
            removals = frozenset(Capability(c) for c in caps)
            if role in roles and removals:
                roles[role] = roles[role] - removals
        return PolicyConfig(roles=roles, tier_overrides=dict(base.tier_overrides))

    def effective_skills(
        self, base: Optional[SkillRegistry]
    ) -> Optional[SkillRegistry]:
        """The skill registry this workstream's roles should use.

        - no ``skills`` config → ``base`` unchanged (behavior-preserving);
        - ``skills.dir`` → discover a workstream-local registry from that dir
          (resolved relative to the workstream directory) instead of ``base``;
        - ``skills.names`` → restrict (whichever registry is in play) to that
          named subset, so a vertical scopes its roles to a curated skill set.
        """
        if self.skills is None:
            return base
        reg = base
        if self.skills.dir:
            reg = SkillRegistry.discover(self._resolve_dir(self.skills.dir))
        if self.skills.names:
            source = reg or SkillRegistry()
            picked = [s for name in self.skills.names if (s := source.get(name))]
            return SkillRegistry(picked)
        return reg

    def _resolve_dir(self, raw: str) -> Path:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
        base = self._source_dir or (_DEFAULT_DIR / self.name)
        return base / p

    def source_dir(self) -> Optional[Path]:
        """The directory the config was loaded from (``None`` if built in memory)."""
        return self._source_dir


# --- loading ----------------------------------------------------------------


def workstreams_dir() -> Path:
    """The workstreams root: ``$AI_STUDIO_WORKSTREAMS_DIR`` or repo ``workstreams/``."""
    env = os.environ.get(_ENV_WORKSTREAMS_DIR)
    return Path(env).expanduser() if env else _DEFAULT_DIR


def config_path(name: str, *, base_dir: Optional[Path] = None) -> Path:
    """The expected config path for workstream ``name`` (``<dir>/<name>/config.yaml``)."""
    root = base_dir if base_dir is not None else workstreams_dir()
    return root / name / CONFIG_FILENAME


def load_config_file(path: str | Path) -> WorkstreamConfig:
    """Load + validate a :class:`WorkstreamConfig` from an explicit YAML path.

    Raises :class:`WorkstreamConfigError` (naming the file) if the file is missing,
    not a mapping, or fails validation (unknown field / capability / period /
    checker), or if its ``name`` does not match its directory.
    """
    p = Path(path)
    if not p.exists():
        raise WorkstreamConfigError(f"no workstream config at {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WorkstreamConfigError(f"{p}: invalid YAML: {exc}") from exc
    if raw is None:
        raise WorkstreamConfigError(f"{p}: config is empty")
    if not isinstance(raw, dict):
        raise WorkstreamConfigError(f"{p}: config must be a mapping, got {type(raw).__name__}")
    try:
        cfg = WorkstreamConfig.model_validate(raw)
    except ValidationError as exc:
        raise WorkstreamConfigError(f"{p}: {exc}") from exc

    cfg._source_dir = p.parent
    # The name must match the directory so lookups by workstream slug are stable.
    dir_name = p.parent.name
    if cfg.name != dir_name:
        raise WorkstreamConfigError(
            f"{p}: config name {cfg.name!r} does not match directory {dir_name!r}"
        )
    return cfg


def load_workstream_config(
    name: str, *, base_dir: Optional[Path] = None
) -> WorkstreamConfig:
    """Load workstream ``name``'s config from ``<dir>/<name>/config.yaml`` (strict)."""
    return load_config_file(config_path(name, base_dir=base_dir))


def resolve_workstream_config(
    name: Optional[str], *, base_dir: Optional[Path] = None
) -> Optional[WorkstreamConfig]:
    """Return workstream ``name``'s config, or ``None`` if it has none.

    The wiring seam the worker uses: a workstream WITHOUT a config file falls back
    to the platform's inline base behavior (behavior-preserving). A config file
    that EXISTS but is malformed still raises — a broken config is a real error,
    not a silent fallback.
    """
    if not name:
        return None
    path = config_path(name, base_dir=base_dir)
    if not path.exists():
        return None
    return load_config_file(path)
