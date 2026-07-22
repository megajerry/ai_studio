"""Capability model and action tiers (architecture §5, CLAUDE.md approval tiers).

A **capability** is a single kind of side effect a tool may need (`fs.read`,
`shell.exec`, …). Every tool declares the capabilities each call requires, and
the policy engine (:mod:`runtime.policy`) gates the call against the calling
role's *granted* capabilities (least privilege) and the capability's **action
tier**:

- 🟢 GREEN  — read / search / summarize              → auto-allow
- 🟡 YELLOW — write file / git commit / create branch → auto-allow, logged
- 🔴 RED    — delete / spend / deploy / shell / SSH   → human approval required

This module is pure (no DB, no I/O) so tier logic is trivially unit-testable.
The default capability→tier map lives here; a policy config may *override* a
capability's tier as data (see :mod:`runtime.policy`), so tiers stay rules-as-data
rather than hardcoded per tool.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """A single side-effect permission a tool call may require.

    String-valued so it round-trips cleanly through YAML policy config and JSON
    event payloads.
    """

    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    SHELL_EXEC = "shell.exec"
    CODE_RUN = "code.run"
    GIT_WRITE = "git.write"
    NET_FETCH = "net.fetch"
    SECRET_USE = "secret.use"
    SPEND_MONEY = "spend.money"
    DEPLOY = "deploy"


class ActionTier(str, Enum):
    """Risk tier of an action (CLAUDE.md, architecture §5)."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


# Ascending severity — used to take the most-restrictive tier across a set of
# capabilities and to compare tiers without string juggling.
_TIER_RANK: dict[ActionTier, int] = {
    ActionTier.GREEN: 0,
    ActionTier.YELLOW: 1,
    ActionTier.RED: 2,
}


def tier_rank(tier: ActionTier) -> int:
    """Numeric severity of a tier (GREEN=0 < YELLOW=1 < RED=2)."""
    return _TIER_RANK[tier]


# Default capability → tier mapping (architecture §5). A policy config can
# override individual entries as data; this is only the built-in baseline.
DEFAULT_CAPABILITY_TIER: dict[Capability, ActionTier] = {
    # 🟢 Green — pure reads / fetches.
    Capability.FS_READ: ActionTier.GREEN,
    Capability.NET_FETCH: ActionTier.GREEN,
    # 🟡 Yellow — reversible local mutations; auto but logged.
    Capability.FS_WRITE: ActionTier.YELLOW,
    Capability.GIT_WRITE: ActionTier.YELLOW,
    Capability.SECRET_USE: ActionTier.YELLOW,
    # 🔴 Red — irreversible / costly / escapes the sandbox; human approval.
    Capability.FS_DELETE: ActionTier.RED,
    Capability.SHELL_EXEC: ActionTier.RED,
    # Running a coding worker (opencode) executes agent-authored code in the
    # sandbox — same escape-the-sandbox risk class as shell.exec, so 🔴.
    Capability.CODE_RUN: ActionTier.RED,
    Capability.SPEND_MONEY: ActionTier.RED,
    Capability.DEPLOY: ActionTier.RED,
}


def tier_for_capability(
    cap: Capability,
    overrides: dict[Capability, ActionTier] | None = None,
) -> ActionTier:
    """Return the action tier of a single capability, honoring config overrides."""
    if overrides and cap in overrides:
        return overrides[cap]
    return DEFAULT_CAPABILITY_TIER[cap]


def effective_tier(
    capabilities: frozenset[Capability] | set[Capability],
    overrides: dict[Capability, ActionTier] | None = None,
) -> ActionTier:
    """Return the most-restrictive tier across a set of capabilities.

    A call that needs several capabilities is gated at the highest tier among
    them (one 🔴 capability makes the whole call 🔴). An empty set is GREEN.
    """
    tier = ActionTier.GREEN
    for cap in capabilities:
        cap_tier = tier_for_capability(cap, overrides)
        if tier_rank(cap_tier) > tier_rank(tier):
            tier = cap_tier
    return tier
