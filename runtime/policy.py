"""Policy engine — the single layer that gates every tool call (architecture §5).

The engine answers, for one requested tool call, the questions from §5:

    Can Read?  Can Write?  Need Approval?  Need Budget?

as a :class:`Decision` of ALLOW / DENY / NEEDS_APPROVAL. The agent never knows
the rules; it acts, and this layer allows / gates / escalates.

**Rules are data, not code.** A :class:`PolicyConfig` (loaded from YAML) holds
`role → granted capabilities` grants and optional per-capability `tier
overrides`. Adding a role or re-tiering a capability is a config edit, not a code
change. The decision logic itself is small and pure (no DB, no I/O), so it is
fully unit-testable.

Decision order:

1. **Least privilege.** Any required capability the role was not granted → DENY.
2. **Budget.** If a budget context is supplied and the call would exceed the cap
   → NEEDS_APPROVAL (raising a budget is a 🛑 stakeholder-approval item, ADR-0006).
3. **Tier.** GREEN → ALLOW; YELLOW → ALLOW (logged); RED → NEEDS_APPROVAL.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from .capabilities import ActionTier, Capability, effective_tier

# Resolution order for the active policy file. The example is the committed,
# documented default; a real policy can live in a git-ignored runtime/policy.yaml
# or be pointed at explicitly via the env var (ADR-0011 keeps real config local).
_ENV_POLICY_PATH = "AI_STUDIO_POLICY_FILE"
_RUNTIME_DIR = Path(__file__).resolve().parent
_LOCAL_POLICY = _RUNTIME_DIR / "policy.yaml"
_EXAMPLE_POLICY = _RUNTIME_DIR / "policy.example.yaml"


class Effect(str, Enum):
    """The three outcomes the policy engine can return."""

    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


class BudgetContext(BaseModel):
    """Optional cost context for a call (ADR-0012 budget enforcement).

    ``would_exceed`` is true when committing this call's estimated cost would
    push cumulative spend past the cap.
    """

    spent_tokens: int = 0
    budget_tokens: Optional[int] = None
    estimated_tokens: int = 0

    @property
    def would_exceed(self) -> bool:
        if self.budget_tokens is None:
            return False
        return self.spent_tokens + self.estimated_tokens > self.budget_tokens


class PolicyRequest(BaseModel):
    """A single authorization question put to the engine."""

    role: str
    tool: str
    required_capabilities: frozenset[Capability]
    budget: Optional[BudgetContext] = None


class Decision(BaseModel):
    """The engine's answer, with enough context to log and to escalate."""

    effect: Effect
    tier: ActionTier
    reason: str
    role: str
    tool: str
    required_capabilities: frozenset[Capability] = Field(default_factory=frozenset)
    missing_capabilities: frozenset[Capability] = Field(default_factory=frozenset)
    #: True for auto-allowed-but-logged (🟡) calls; the log IS the audit record.
    logged: bool = False

    def to_payload(self) -> dict:
        """JSON-serializable summary for the event log."""
        return {
            "effect": self.effect.value,
            "tier": self.tier.value,
            "reason": self.reason,
            "role": self.role,
            "tool": self.tool,
            "required_capabilities": sorted(c.value for c in self.required_capabilities),
            "missing_capabilities": sorted(c.value for c in self.missing_capabilities),
            "logged": self.logged,
        }


class PolicyConfig(BaseModel):
    """Policy rules as data: role grants + tier overrides."""

    #: role → the capabilities that role is granted (least privilege).
    roles: dict[str, frozenset[Capability]] = Field(default_factory=dict)
    #: capability → tier, overriding the built-in default tier.
    tier_overrides: dict[Capability, ActionTier] = Field(default_factory=dict)

    def granted(self, role: str) -> frozenset[Capability]:
        """Capabilities granted to ``role`` (empty for an unknown role)."""
        return self.roles.get(role, frozenset())

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyConfig":
        roles_raw = (data or {}).get("roles", {}) or {}
        roles = {
            role: frozenset(Capability(c) for c in caps)
            for role, caps in roles_raw.items()
        }
        overrides_raw = (data or {}).get("tier_overrides", {}) or {}
        tier_overrides = {
            Capability(cap): ActionTier(tier) for cap, tier in overrides_raw.items()
        }
        return cls(roles=roles, tier_overrides=tier_overrides)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PolicyConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})


def resolve_policy_path() -> Path:
    """Return the active policy file per the resolution order (env → local → example)."""
    env_path = os.environ.get(_ENV_POLICY_PATH)
    if env_path:
        return Path(env_path)
    if _LOCAL_POLICY.exists():
        return _LOCAL_POLICY
    return _EXAMPLE_POLICY


def load_policy(path: str | Path | None = None) -> PolicyConfig:
    """Load a :class:`PolicyConfig`, defaulting to :func:`resolve_policy_path`."""
    return PolicyConfig.from_yaml(path if path is not None else resolve_policy_path())


def decide(request: PolicyRequest, config: PolicyConfig) -> Decision:
    """Authorize one tool call. Pure: no I/O, no events (see :mod:`runtime.enforce`)."""
    granted = config.granted(request.role)
    tier = effective_tier(request.required_capabilities, config.tier_overrides)

    common = dict(
        tier=tier,
        role=request.role,
        tool=request.tool,
        required_capabilities=request.required_capabilities,
    )

    # 1. Least privilege — deny before considering tier or budget.
    missing = frozenset(request.required_capabilities - granted)
    if missing:
        return Decision(
            effect=Effect.DENY,
            reason=(
                f"role {request.role!r} lacks capabilities: "
                f"{sorted(c.value for c in missing)}"
            ),
            missing_capabilities=missing,
            **common,
        )

    # 2. Budget — an over-budget call escalates for approval (ADR-0006/0012).
    if request.budget is not None and request.budget.would_exceed:
        return Decision(
            effect=Effect.NEEDS_APPROVAL,
            reason=(
                "budget would be exceeded "
                f"({request.budget.spent_tokens}+{request.budget.estimated_tokens} "
                f"> {request.budget.budget_tokens})"
            ),
            **common,
        )

    # 3. Tier.
    if tier is ActionTier.RED:
        return Decision(
            effect=Effect.NEEDS_APPROVAL,
            reason="🔴 red-tier action requires human approval",
            **common,
        )
    if tier is ActionTier.YELLOW:
        return Decision(
            effect=Effect.ALLOW,
            reason="🟡 yellow-tier action auto-allowed (logged)",
            logged=True,
            **common,
        )
    return Decision(
        effect=Effect.ALLOW,
        reason="🟢 green-tier action auto-allowed",
        **common,
    )
