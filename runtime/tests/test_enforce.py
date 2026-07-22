"""Enforced invocation path — the policy gate around tool execution (no DB).

Uses MemoryEventSink so event emission is asserted without a database.
"""

from __future__ import annotations

import pytest

from runtime.capabilities import Capability
from runtime.enforce import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_POLICY_DECISION,
    EVENT_TOOL_INVOKED,
    InvokeStatus,
    MemoryEventSink,
    invoke,
)
from runtime.policy import BudgetContext, PolicyConfig
from runtime.tools import ToolRegistry
from runtime.tools.filesystem import FilesystemTool
from runtime.tools.shell import ShellTool

CONFIG = PolicyConfig(
    roles={
        "researcher": frozenset({Capability.FS_READ}),
        "builder": frozenset({Capability.FS_READ, Capability.FS_WRITE}),
        "admin": frozenset(
            {Capability.FS_READ, Capability.FS_WRITE, Capability.FS_DELETE}
        ),
        "deployer": frozenset({Capability.SHELL_EXEC}),
    }
)


@pytest.fixture
def registry(tmp_path):
    (tmp_path / "hello.txt").write_text("hi")
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=tmp_path))
    reg.register(ShellTool())  # no sandbox
    return reg, tmp_path


def _invoke(role, tool, registry, sink, **kw):
    return invoke(role, tool, registry=registry, config=CONFIG, events=sink, **kw)


def test_green_read_executes_and_emits_events(registry):
    reg, _ = registry
    sink = MemoryEventSink()
    res = _invoke("researcher", "filesystem", reg, sink, op="read", path="hello.txt")
    assert res.status is InvokeStatus.EXECUTED
    assert res.result.ok and res.result.output == "hi"
    assert sink.types() == [EVENT_POLICY_DECISION, EVENT_TOOL_INVOKED]


def test_yellow_write_executes(registry):
    reg, tmp = registry
    sink = MemoryEventSink()
    res = _invoke("builder", "filesystem", reg, sink, op="write", path="w.txt", content="x")
    assert res.status is InvokeStatus.EXECUTED
    assert (tmp / "w.txt").read_text() == "x"
    assert res.decision.logged is True


def test_least_privilege_deny_does_not_execute(registry):
    reg, tmp = registry
    sink = MemoryEventSink()
    # researcher lacks fs.write
    res = _invoke("researcher", "filesystem", reg, sink, op="write", path="x.txt", content="x")
    assert res.status is InvokeStatus.DENIED
    assert res.result is None
    assert not (tmp / "x.txt").exists()
    assert sink.types() == [EVENT_POLICY_DECISION]  # no tool.invoked


def test_red_delete_pends_and_never_executes(registry):
    reg, tmp = registry
    sink = MemoryEventSink()
    res = _invoke("admin", "filesystem", reg, sink, op="delete", path="hello.txt")
    assert res.status is InvokeStatus.PENDING
    assert res.result is None
    assert res.approval_id is not None
    # File must still exist — RED never auto-executes.
    assert (tmp / "hello.txt").exists()
    assert sink.types() == [EVENT_POLICY_DECISION, EVENT_APPROVAL_REQUESTED]
    approval = sink.events[-1]
    assert approval.payload["approval_id"] == str(res.approval_id)
    assert approval.payload["tier"] == "red"


def test_red_shell_pends_without_touching_host(registry):
    reg, _ = registry
    sink = MemoryEventSink()
    res = _invoke("deployer", "shell", reg, sink, command="rm -rf /")
    assert res.status is InvokeStatus.PENDING
    assert res.result is None  # tool.execute never called
    assert sink.types() == [EVENT_POLICY_DECISION, EVENT_APPROVAL_REQUESTED]


def test_budget_exceeded_pends(registry):
    reg, _ = registry
    sink = MemoryEventSink()
    budget = BudgetContext(spent_tokens=1000, budget_tokens=1000, estimated_tokens=1)
    res = _invoke(
        "researcher", "filesystem", reg, sink, budget=budget, op="read", path="hello.txt"
    )
    assert res.status is InvokeStatus.PENDING


def test_unknown_tool_denied(registry):
    reg, _ = registry
    sink = MemoryEventSink()
    res = invoke("researcher", "ghost", registry=reg, config=CONFIG, events=sink)
    assert res.status is InvokeStatus.DENIED
    assert res.result is None
    assert sink.types() == [EVENT_POLICY_DECISION]


def test_event_payload_never_contains_arg_values(registry):
    # Only argument *keys* are logged, never values (no secrets/content leak).
    reg, _ = registry
    sink = MemoryEventSink()
    _invoke("builder", "filesystem", reg, sink, op="write", path="s.txt", content="SENSITIVE")
    for ev in sink.events:
        assert "SENSITIVE" not in str(ev.payload)
        assert "arg_keys" in ev.payload
