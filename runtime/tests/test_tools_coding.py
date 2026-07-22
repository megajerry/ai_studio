"""CodingTool — dispatch a coding worker (opencode) INSIDE the sandbox only.

Docker/opencode are NEVER launched: the sandbox is a fake and the underlying
DockerSandboxRunner's ``build_invocation`` is asserted purely (no subprocess).
Covers: declares ``code.run`` (🔴); refuses without a sandbox (never runs on the
host); builds the correct ``opencode run <goal>`` invocation inside the sandbox;
opencode is swappable via ``CODING_WORKER_CMD`` / arg; the env allowlist forwards
only allowlisted names and leaks no host secret; and — through the enforced
``invoke`` gate — it PENDs (NEEDS_APPROVAL) without a grant and EXECUTES with one.
"""

from __future__ import annotations

import pytest

from runtime.capabilities import Capability
from runtime.enforce import InvokeStatus, MemoryEventSink, invoke
from runtime.policy import PolicyConfig
from runtime.sandbox import CONTAINER_WORKDIR
from runtime.tools import CodingTool, ToolRegistry


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class _FakeSandbox:
    """Records every command; never touches the host."""

    def __init__(self, result=(0, "built prototype", "")):
        self.ran: list[str] = []
        self._result = result

    def run(self, command, **kwargs):
        self.ran.append(command)
        return self._result


# --------------------------------------------------------------------------- #
# capability + tier
# --------------------------------------------------------------------------- #

def test_declares_code_run_capability():
    assert CodingTool().required_capabilities == frozenset({Capability.CODE_RUN})


def test_code_run_is_red_tier():
    from runtime.capabilities import ActionTier, tier_for_capability

    assert tier_for_capability(Capability.CODE_RUN) is ActionTier.RED


# --------------------------------------------------------------------------- #
# refuse-without-sandbox (never runs on the host)
# --------------------------------------------------------------------------- #

def test_refuses_without_sandbox():
    r = CodingTool().execute(goal="build a landing page")
    assert r.ok is False
    assert "sandbox not configured" in r.error
    assert r.metadata.get("sandboxed") is False
    assert r.output is None  # nothing ran on the host


def test_requires_goal():
    r = CodingTool(sandbox=_FakeSandbox()).execute(goal="")
    assert r.ok is False and "goal" in r.error


# --------------------------------------------------------------------------- #
# runs opencode INSIDE the sandbox (not the host)
# --------------------------------------------------------------------------- #

def test_runs_opencode_inside_the_sandbox():
    sb = _FakeSandbox()
    r = CodingTool(sandbox=sb).execute(goal="scaffold a REST API", workspace="/scratch/ws1")
    assert r.ok is True
    assert r.metadata["sandboxed"] is True
    # opencode was the command run inside the sandbox — never a host subprocess.
    assert sb.ran == ["opencode run 'scaffold a REST API'"]
    assert r.output["produced_files"] == "/scratch/ws1"
    assert r.output["exit_code"] == 0


def test_goal_is_shell_quoted_single_argument():
    sb = _FakeSandbox()
    CodingTool(sandbox=sb).execute(goal="make $(rm -rf /) safe")
    # The goal is quoted so it cannot break out into a second command.
    assert sb.ran == ["opencode run 'make $(rm -rf /) safe'"]


def test_nonzero_exit_is_failure():
    sb = _FakeSandbox(result=(2, "", "worker error"))
    r = CodingTool(sandbox=sb).execute(goal="do a thing")
    assert r.ok is False and r.error == "worker error"
    assert r.output["exit_code"] == 2


# --------------------------------------------------------------------------- #
# opencode is swappable
# --------------------------------------------------------------------------- #

def test_worker_cmd_swappable_via_arg():
    sb = _FakeSandbox()
    CodingTool(sandbox=sb, worker_cmd="claude-code").execute(goal="ship")
    assert sb.ran == ["claude-code run ship"]


def test_worker_cmd_swappable_via_env(monkeypatch):
    monkeypatch.setenv("CODING_WORKER_CMD", "gemini-cli")
    sb = _FakeSandbox()
    tool = CodingTool(sandbox=sb)
    assert tool.worker_cmd == "gemini-cli"
    tool.execute(goal="ship")
    assert sb.ran == ["gemini-cli run ship"]


def test_default_worker_cmd_is_opencode(monkeypatch):
    monkeypatch.delenv("CODING_WORKER_CMD", raising=False)
    assert CodingTool().worker_cmd == "opencode"


# --------------------------------------------------------------------------- #
# env allowlist — no host secrets leak (reuses the sandbox's env handling)
# --------------------------------------------------------------------------- #

def test_env_allowlist_forwards_only_allowlisted_names_no_secret_leak():
    env = {"BUILD_ID": "42", "OPENAI_API_KEY": "sk-SECRET", "PATH": "/usr/bin"}
    tool = CodingTool.with_docker_sandbox(allowed_env=["BUILD_ID"], env=env)
    argv, cli_env = tool.sandbox.build_invocation(tool.build_command("build it"), "cN")
    text = " ".join(argv)

    # opencode runs inside the container, in the mounted workspace convention.
    assert "opencode run 'build it'" in argv
    assert argv[-3:] == ["sh", "-c", "opencode run 'build it'"]
    # Only the allowlisted var is forwarded, and BY NAME (value never in argv).
    assert "-e" in argv and "BUILD_ID" in argv
    assert "OPENAI_API_KEY" not in argv
    assert "sk-SECRET" not in text
    # The docker client env carries the allowlisted value but not the secret.
    assert cli_env.get("BUILD_ID") == "42"
    assert "OPENAI_API_KEY" not in cli_env
    assert CONTAINER_WORKDIR  # sanity: mounted-workdir convention exists


# --------------------------------------------------------------------------- #
# 🔴 through the enforced invoke gate: PEND without a grant, EXECUTE with one
# --------------------------------------------------------------------------- #

_CONFIG = PolicyConfig(roles={"builder": frozenset({Capability.CODE_RUN})})


def _registry(sandbox=None):
    reg = ToolRegistry()
    reg.register(CodingTool(sandbox=sandbox))
    return reg


def test_invoke_pends_without_grant_and_never_runs_worker():
    sb = _FakeSandbox()
    reg = _registry(sandbox=sb)
    sink = MemoryEventSink()
    res = invoke("builder", "coding", registry=reg, config=_CONFIG, events=sink,
                 goal="build a prototype")
    assert res.status is InvokeStatus.PENDING  # 🔴 needs human approval
    assert res.result is None
    assert res.approval_id is not None
    assert sb.ran == []  # opencode NEVER ran — no grant, no execution
    assert res.decision.tier.value == "red"


def test_invoke_denies_role_without_code_run():
    reg = _registry(sandbox=_FakeSandbox())
    sink = MemoryEventSink()
    # A role granted only fs.read cannot dispatch a coding worker.
    cfg = PolicyConfig(roles={"pm": frozenset({Capability.FS_READ})})
    res = invoke("pm", "coding", registry=reg, config=cfg, events=sink, goal="x")
    assert res.status is InvokeStatus.DENIED
    assert res.result is None


def test_invoke_executes_with_a_live_grant(monkeypatch):
    """A human-resolved grant turns the 🔴 dispatch into a one-shot execution:
    opencode then runs INSIDE the sandbox (mocked). No DB — the grant lookup is
    stubbed to simulate an approved, live grant (the enforce.py grant path)."""
    from uuid import uuid4

    import runtime.enforce as enforce

    class _Grant:
        id = uuid4()

    monkeypatch.setattr(enforce, "find_grant", lambda conn, fp: _Grant())
    monkeypatch.setattr(enforce, "consume_grant", lambda conn, gid: gid)

    sb = _FakeSandbox()
    reg = _registry(sandbox=sb)
    sink = MemoryEventSink()
    res = invoke("builder", "coding", registry=reg, config=_CONFIG, events=sink,
                 conn=object(), goal="ship it", workspace="/scratch/ws")
    assert res.status is InvokeStatus.EXECUTED
    assert res.result.ok is True
    assert sb.ran == ["opencode run 'ship it'"]  # ran only after the grant
    assert res.result.output["produced_files"] == "/scratch/ws"
