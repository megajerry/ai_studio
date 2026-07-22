"""Capability → tier mapping (pure, no DB)."""

from __future__ import annotations

from runtime.capabilities import (
    DEFAULT_CAPABILITY_TIER,
    ActionTier,
    Capability,
    effective_tier,
    tier_for_capability,
    tier_rank,
)


def test_every_capability_has_a_default_tier():
    for cap in Capability:
        assert cap in DEFAULT_CAPABILITY_TIER


def test_green_yellow_red_baseline():
    assert tier_for_capability(Capability.FS_READ) is ActionTier.GREEN
    assert tier_for_capability(Capability.NET_FETCH) is ActionTier.GREEN
    assert tier_for_capability(Capability.FS_WRITE) is ActionTier.YELLOW
    assert tier_for_capability(Capability.GIT_WRITE) is ActionTier.YELLOW
    assert tier_for_capability(Capability.FS_DELETE) is ActionTier.RED
    assert tier_for_capability(Capability.SHELL_EXEC) is ActionTier.RED
    assert tier_for_capability(Capability.SPEND_MONEY) is ActionTier.RED
    assert tier_for_capability(Capability.DEPLOY) is ActionTier.RED


def test_tier_rank_orders_green_yellow_red():
    assert tier_rank(ActionTier.GREEN) < tier_rank(ActionTier.YELLOW) < tier_rank(
        ActionTier.RED
    )


def test_effective_tier_is_most_restrictive():
    caps = {Capability.FS_READ, Capability.FS_WRITE, Capability.FS_DELETE}
    assert effective_tier(caps) is ActionTier.RED
    caps = {Capability.FS_READ, Capability.FS_WRITE}
    assert effective_tier(caps) is ActionTier.YELLOW
    assert effective_tier({Capability.FS_READ}) is ActionTier.GREEN


def test_effective_tier_empty_is_green():
    assert effective_tier(set()) is ActionTier.GREEN


def test_tier_override_applied():
    overrides = {Capability.NET_FETCH: ActionTier.YELLOW}
    assert tier_for_capability(Capability.NET_FETCH, overrides) is ActionTier.YELLOW
    assert effective_tier({Capability.NET_FETCH}, overrides) is ActionTier.YELLOW
