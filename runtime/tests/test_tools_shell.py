"""ShellTool — refuses to run unsandboxed; runs only via an injected sandbox."""

from __future__ import annotations

from runtime.capabilities import Capability
from runtime.tools.shell import ShellTool


def test_declares_shell_exec_capability():
    assert ShellTool().required_capabilities == frozenset({Capability.SHELL_EXEC})


def test_refuses_without_sandbox():
    r = ShellTool().execute(command="echo hi")
    assert r.ok is False
    assert "sandbox not configured" in r.error
    assert r.metadata.get("sandboxed") is False
    # No output means nothing ran on the host.
    assert r.output is None


def test_requires_command():
    r = ShellTool().execute(command="")
    assert r.ok is False and "command" in r.error


class _FakeSandbox:
    def __init__(self):
        self.ran = []

    def run(self, command, **kwargs):
        self.ran.append(command)
        return (0, f"ran: {command}", "")


def test_runs_inside_injected_sandbox():
    sandbox = _FakeSandbox()
    r = ShellTool(sandbox=sandbox).execute(command="ls")
    assert r.ok is True
    assert r.metadata["sandboxed"] is True
    assert sandbox.ran == ["ls"]
    assert r.output["stdout"] == "ran: ls"


def test_sandbox_nonzero_exit_is_failure():
    class Failing:
        def run(self, command, **kwargs):
            return (1, "", "boom")

    r = ShellTool(sandbox=Failing()).execute(command="false")
    assert r.ok is False and r.error == "boom"
