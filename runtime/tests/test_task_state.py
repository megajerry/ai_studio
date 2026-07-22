"""Pure unit tests for the canonical lifecycle state machine (ADR-0015). No DB."""

from __future__ import annotations

import pytest

from runtime.models import TaskStatus
from runtime.task_state import (
    STATES,
    TERMINAL,
    TRANSITIONS,
    DependencyCycle,
    IllegalTransition,
    assert_acyclic,
    assert_transition,
    can_transition,
    is_terminal,
)


def test_states_cover_the_canonical_set():
    assert STATES == {
        "up_for_grabs", "claimed", "in_progress", "blocked",
        "ready_for_review", "reviewer_blocked", "approved", "merged", "abandoned",
    }
    assert TERMINAL == {"merged", "abandoned"}


def test_forward_lifecycle_is_legal():
    legal = [
        ("up_for_grabs", "claimed"),
        ("claimed", "in_progress"),
        ("in_progress", "ready_for_review"),
        ("ready_for_review", "approved"),
        ("approved", "merged"),
        ("in_progress", "blocked"),
        ("blocked", "in_progress"),
        ("ready_for_review", "reviewer_blocked"),
        ("reviewer_blocked", "in_progress"),
    ]
    for a, b in legal:
        assert can_transition(a, b), f"{a}->{b} should be legal"
        assert_transition(a, b)  # does not raise


def test_every_nonterminal_can_be_abandoned():
    for s in STATES - TERMINAL:
        assert can_transition(s, "abandoned"), f"{s} should be abandonable"


@pytest.mark.parametrize(
    "a,b",
    [
        ("up_for_grabs", "merged"),      # can't skip the whole lifecycle
        ("up_for_grabs", "in_progress"), # must be claimed first
        ("in_progress", "merged"),       # work must be reviewed + approved
        ("ready_for_review", "merged"),  # must be approved first
        ("merged", "in_progress"),       # terminal — no outgoing edges
        ("abandoned", "up_for_grabs"),   # terminal — no outgoing edges
        ("claimed", "merged"),
    ],
)
def test_illegal_transitions_are_rejected(a, b):
    assert not can_transition(a, b)
    with pytest.raises(IllegalTransition):
        assert_transition(a, b)


def test_terminal_states_have_no_outgoing_edges():
    assert TRANSITIONS["merged"] == set()
    assert TRANSITIONS["abandoned"] == set()
    assert is_terminal(TaskStatus.MERGED) and is_terminal("abandoned")
    assert not is_terminal(TaskStatus.IN_PROGRESS)


def test_recovery_edges_present():
    # Documented operational recovery edges (supervisor re-kick + approval re-queue).
    assert can_transition("in_progress", "up_for_grabs")
    assert can_transition("blocked", "up_for_grabs")
    assert can_transition("claimed", "up_for_grabs")


def test_unknown_status_rejected():
    with pytest.raises(IllegalTransition):
        assert_transition("bogus", "merged")
    with pytest.raises(IllegalTransition):
        assert_transition("in_progress", "bogus")


# --- dependency-graph safety ------------------------------------------------


def test_assert_acyclic_accepts_chain_and_diamond():
    assert_acyclic({1: [], 2: [1], 3: [2]})           # chain
    assert_acyclic({1: [], 2: [1], 3: [1], 4: [2, 3]})  # diamond


def test_assert_acyclic_rejects_self_dependency():
    with pytest.raises(DependencyCycle):
        assert_acyclic({1: [1]})


def test_assert_acyclic_rejects_cycle():
    with pytest.raises(DependencyCycle):
        assert_acyclic({1: [2], 2: [3], 3: [1]})
