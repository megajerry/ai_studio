"""Policy engine decisions (pure, no DB)."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.capabilities import ActionTier, Capability
from runtime.policy import (
    BudgetContext,
    Effect,
    PolicyConfig,
    PolicyRequest,
    decide,
    load_policy,
)

RESEARCHER = frozenset({Capability.FS_READ, Capability.NET_FETCH})
BUILDER = frozenset({Capability.FS_READ, Capability.FS_WRITE, Capability.GIT_WRITE})
DEPLOYER = frozenset(
    {
        Capability.FS_READ,
        Capability.FS_WRITE,
        Capability.GIT_WRITE,
        Capability.DEPLOY,
        Capability.SHELL_EXEC,
    }
)

CONFIG = PolicyConfig(
    roles={"researcher": RESEARCHER, "builder": BUILDER, "deployer": DEPLOYER}
)


def _req(role, caps, budget=None):
    return PolicyRequest(
        role=role, tool="t", required_capabilities=frozenset(caps), budget=budget
    )


def test_green_allows():
    d = decide(_req("researcher", {Capability.FS_READ}), CONFIG)
    assert d.effect is Effect.ALLOW
    assert d.tier is ActionTier.GREEN
    assert d.logged is False


def test_yellow_allows_and_flags_logged():
    d = decide(_req("builder", {Capability.FS_WRITE}), CONFIG)
    assert d.effect is Effect.ALLOW
    assert d.tier is ActionTier.YELLOW
    assert d.logged is True


def test_red_needs_approval():
    d = decide(_req("deployer", {Capability.DEPLOY}), CONFIG)
    assert d.effect is Effect.NEEDS_APPROVAL
    assert d.tier is ActionTier.RED


def test_least_privilege_denies_missing_capability():
    # Researcher was never granted fs.write.
    d = decide(_req("researcher", {Capability.FS_WRITE}), CONFIG)
    assert d.effect is Effect.DENY
    assert Capability.FS_WRITE in d.missing_capabilities


def test_unknown_role_denied():
    d = decide(_req("ghost", {Capability.FS_READ}), CONFIG)
    assert d.effect is Effect.DENY


def test_least_privilege_checked_before_tier():
    # A green capability the role lacks still denies (not allow).
    cfg = PolicyConfig(roles={"nobody": frozenset()})
    d = decide(_req("nobody", {Capability.FS_READ}), cfg)
    assert d.effect is Effect.DENY


def test_budget_exceeded_needs_approval():
    budget = BudgetContext(spent_tokens=900, budget_tokens=1000, estimated_tokens=200)
    d = decide(_req("builder", {Capability.FS_WRITE}, budget=budget), CONFIG)
    assert d.effect is Effect.NEEDS_APPROVAL
    assert "budget" in d.reason


def test_budget_within_cap_allows():
    budget = BudgetContext(spent_tokens=100, budget_tokens=1000, estimated_tokens=200)
    d = decide(_req("builder", {Capability.FS_WRITE}, budget=budget), CONFIG)
    assert d.effect is Effect.ALLOW


def test_budget_deny_takes_precedence_over_budget():
    # Missing capability denies even if a budget is also blown.
    budget = BudgetContext(spent_tokens=900, budget_tokens=1000, estimated_tokens=200)
    d = decide(_req("researcher", {Capability.DEPLOY}, budget=budget), CONFIG)
    assert d.effect is Effect.DENY


def test_multi_capability_gated_at_highest_tier():
    d = decide(
        _req("deployer", {Capability.FS_READ, Capability.SHELL_EXEC}), CONFIG
    )
    assert d.tier is ActionTier.RED
    assert d.effect is Effect.NEEDS_APPROVAL


def test_tier_override_from_config_changes_decision():
    cfg = PolicyConfig(
        roles={"researcher": RESEARCHER},
        tier_overrides={Capability.NET_FETCH: ActionTier.RED},
    )
    d = decide(_req("researcher", {Capability.NET_FETCH}), cfg)
    assert d.effect is Effect.NEEDS_APPROVAL


def test_decision_payload_is_json_safe():
    d = decide(_req("deployer", {Capability.DEPLOY}), CONFIG)
    payload = d.to_payload()
    assert payload["effect"] == "needs_approval"
    assert payload["tier"] == "red"
    assert "deploy" in payload["required_capabilities"]


# --- config loading ---------------------------------------------------------


def test_example_policy_loads_and_enforces_least_privilege():
    example = Path(__file__).resolve().parents[1] / "policy.example.yaml"
    cfg = load_policy(example)
    assert Capability.FS_READ in cfg.granted("researcher")
    # Researcher is not granted write in the shipped example.
    assert Capability.FS_WRITE not in cfg.granted("researcher")
    # Builder can write.
    assert Capability.FS_WRITE in cfg.granted("builder")


def test_policy_from_dict_parses_roles_and_overrides():
    cfg = PolicyConfig.from_dict(
        {
            "roles": {"r": ["fs.read", "net.fetch"]},
            "tier_overrides": {"net.fetch": "yellow"},
        }
    )
    assert cfg.granted("r") == frozenset({Capability.FS_READ, Capability.NET_FETCH})
    assert cfg.tier_overrides[Capability.NET_FETCH] is ActionTier.YELLOW


def test_policy_from_dict_rejects_unknown_capability():
    with pytest.raises(ValueError):
        PolicyConfig.from_dict({"roles": {"r": ["fs.teleport"]}})
