"""DockerSandboxRunner + ShellTool integration — Docker fully MOCKED.

Asserts the constructed ``docker run`` invocation enforces every safety default
(network off, throwaway, non-root, resource + pid ceilings, dropped caps,
read-only rootfs, scoped mount, no host-root mount, allowlisted env only), that a
timeout force-kills the container, and that ShellTool still refuses with no runner
and runs through the runner (still 🔴) when one is configured — without ever
launching a real container.
"""

from __future__ import annotations

import subprocess

import pytest

from runtime.capabilities import Capability
from runtime.sandbox import CONTAINER_WORKDIR, TIMEOUT_EXIT_CODE
from runtime.sandbox.docker import DockerSandboxRunner, SandboxConfigError
from runtime.tools.shell import ShellTool


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _completed(returncode=0, stdout="out", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _Recorder:
    """Stand-in for ``subprocess.run`` that records every call."""

    def __init__(self, result=None, side_effect=None):
        self.calls: list[dict] = []
        self._result = result if result is not None else _completed()
        self._side_effect = side_effect

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self._side_effect is not None:
            return self._side_effect(argv, **kwargs)
        return self._result


def _flags(argv: list[str]) -> str:
    return " ".join(argv)


# --------------------------------------------------------------------------- #
# command construction — safety defaults
# --------------------------------------------------------------------------- #

def test_defaults_are_strong():
    r = DockerSandboxRunner(env={})
    argv, _ = r.build_invocation("echo hi", "cN")
    text = _flags(argv)

    # throwaway + no network
    assert "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    # non-root
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert "0:0" not in argv and "root" not in argv
    # resource ceilings
    assert argv[argv.index("--memory") + 1] == "256m"
    assert argv[argv.index("--cpus") + 1] == "1.0"
    assert argv[argv.index("--pids-limit") + 1] == "256"
    # dropped privileges
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in text
    # immutable rootfs + writable tmp only
    assert "--read-only" in argv
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    # the command is passed explicitly, not via an image ENTRYPOINT
    assert argv[-3:] == ["sh", "-c", "echo hi"]
    assert "ai-studio-sandbox:latest" in argv


def test_network_and_limits_are_configurable():
    r = DockerSandboxRunner(
        env={},
        network="bridge",
        memory="512m",
        cpus="2",
        pids_limit=64,
        image="custom:tag",
    )
    argv, _ = r.build_invocation("true", "cN")
    assert argv[argv.index("--network") + 1] == "bridge"
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--pids-limit") + 1] == "64"
    assert "custom:tag" in argv


def test_config_from_env():
    env = {
        "SANDBOX_IMAGE": "img:1",
        "SANDBOX_NETWORK": "none",
        "SANDBOX_MEMORY": "128m",
        "SANDBOX_CPUS": "0.5",
        "SANDBOX_TIMEOUT_S": "7",
    }
    r = DockerSandboxRunner(env=env)
    assert r.image == "img:1"
    assert r.memory == "128m"
    assert r.cpus == "0.5"
    assert r.timeout_s == 7.0


# --------------------------------------------------------------------------- #
# scoped mount / no host-root mount
# --------------------------------------------------------------------------- #

def test_scoped_workdir_mount(tmp_path):
    r = DockerSandboxRunner(env={}, workdir=str(tmp_path))
    argv, _ = r.build_invocation("ls", "cN")
    mount = f"{tmp_path.resolve()}:{CONTAINER_WORKDIR}:rw"
    assert argv[argv.index("-v") + 1] == mount
    assert argv[argv.index("-w") + 1] == CONTAINER_WORKDIR


def test_workdir_can_be_readonly(tmp_path):
    r = DockerSandboxRunner(env={}, workdir=str(tmp_path), workdir_readonly=True)
    argv, _ = r.build_invocation("ls", "cN")
    assert argv[argv.index("-v") + 1].endswith(":ro")


def test_no_mount_without_workdir():
    r = DockerSandboxRunner(env={})
    argv, _ = r.build_invocation("ls", "cN")
    assert "-v" not in argv


def test_refuses_to_mount_host_root():
    with pytest.raises(SandboxConfigError):
        DockerSandboxRunner(env={}, workdir="/")


def test_workdir_is_scoped_not_host_root(tmp_path):
    # Sanity: the mounted host path is the scoped dir, never '/'.
    r = DockerSandboxRunner(env={}, workdir=str(tmp_path))
    argv, _ = r.build_invocation("ls", "cN")
    host_side = argv[argv.index("-v") + 1].split(":")[0]
    assert host_side != "/"
    assert host_side == str(tmp_path.resolve())


# --------------------------------------------------------------------------- #
# env / secret isolation
# --------------------------------------------------------------------------- #

def test_no_host_secret_env_forwarded_by_default():
    env = {"PATH": "/usr/bin", "AWS_SECRET_ACCESS_KEY": "shhh", "OPENAI_API_KEY": "sk-xxx"}
    r = DockerSandboxRunner(env=env)  # allowed_env defaults to none
    argv, cli_env = r.build_invocation("env", "cN")
    # No -e forwarding at all.
    assert "-e" not in argv
    # Secret value appears nowhere in the argv...
    assert not any("shhh" in a or "sk-xxx" in a for a in argv)
    # ...nor in the environment handed to the docker client.
    assert "AWS_SECRET_ACCESS_KEY" not in cli_env
    assert "OPENAI_API_KEY" not in cli_env
    # PATH (operational) IS available to the client but is not -e'd into container.
    assert cli_env.get("PATH") == "/usr/bin"


def test_only_allowlisted_env_forwarded():
    env = {
        "PATH": "/usr/bin",
        "BUILD_ID": "42",
        "AWS_SECRET_ACCESS_KEY": "shhh",
    }
    r = DockerSandboxRunner(env=env, allowed_env=["BUILD_ID"])
    argv, cli_env = r.build_invocation("env", "cN")
    # Allowlisted var forwarded by NAME only (value not in argv → not on `ps`).
    assert "-e" in argv and argv[argv.index("-e") + 1] == "BUILD_ID"
    assert "42" not in argv
    # Its value is available to the client so docker can resolve the name.
    assert cli_env["BUILD_ID"] == "42"
    # The secret is still withheld everywhere.
    assert "AWS_SECRET_ACCESS_KEY" not in cli_env
    assert not any("shhh" in a for a in argv)


def test_allowlisted_but_absent_env_is_not_forwarded():
    r = DockerSandboxRunner(env={"PATH": "/usr/bin"}, allowed_env=["NOT_SET"])
    argv, cli_env = r.build_invocation("env", "cN")
    assert "-e" not in argv
    assert "NOT_SET" not in cli_env


# --------------------------------------------------------------------------- #
# execution — mocked docker CLI
# --------------------------------------------------------------------------- #

def test_run_invokes_docker_with_timeout(monkeypatch):
    rec = _Recorder(result=_completed(returncode=0, stdout="hello\n", stderr=""))
    monkeypatch.setattr(subprocess, "run", rec)

    r = DockerSandboxRunner(env={"PATH": "/usr/bin"}, timeout_s=12)
    code, out, err = r.run("echo hello")

    assert (code, out, err) == (0, "hello\n", "")
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["argv"][0] == "docker" and call["argv"][1] == "run"
    assert call["kwargs"]["timeout"] == 12
    assert call["kwargs"]["capture_output"] is True
    # docker client env carries no secrets (only operational passthrough here).
    assert call["kwargs"]["env"].get("PATH") == "/usr/bin"


def test_run_propagates_nonzero_exit(monkeypatch):
    rec = _Recorder(result=_completed(returncode=2, stdout="", stderr="boom"))
    monkeypatch.setattr(subprocess, "run", rec)
    code, out, err = DockerSandboxRunner(env={}).run("false")
    assert code == 2 and err == "boom"


def test_timeout_kills_container(monkeypatch):
    calls: list[list[str]] = []

    def side_effect(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))
        return _completed()  # the `docker rm -f` cleanup

    monkeypatch.setattr(subprocess, "run", _Recorder(side_effect=side_effect))

    r = DockerSandboxRunner(env={"PATH": "/usr/bin"}, timeout_s=1)
    code, out, err = r.run("sleep 999")

    assert code == TIMEOUT_EXIT_CODE
    assert "timeout" in err.lower()
    # A run then a force-remove of the SAME container name.
    run_call = next(c for c in calls if c[:2] == ["docker", "run"])
    rm_call = next(c for c in calls if c[:3] == ["docker", "rm", "-f"])
    container_name = run_call[run_call.index("--name") + 1]
    assert rm_call[-1] == container_name


def test_run_never_touches_real_docker_here(monkeypatch):
    # Guard: if the mock is missing, the test must fail loudly rather than shell
    # out. Patch with something that raises to prove the code path is the mock.
    def explode(*a, **k):
        raise AssertionError("real subprocess.run should not be reached in tests")

    monkeypatch.setattr(subprocess, "run", explode)
    with pytest.raises(AssertionError):
        DockerSandboxRunner(env={}).run("echo hi")


# --------------------------------------------------------------------------- #
# ShellTool integration — gate preserved
# --------------------------------------------------------------------------- #

def test_shelltool_still_red_tier():
    # The 🔴 policy gate is unchanged: shell.exec is still the required capability.
    assert ShellTool().required_capabilities == frozenset({Capability.SHELL_EXEC})
    assert ShellTool.with_docker_sandbox(env={}).required_capabilities == frozenset(
        {Capability.SHELL_EXEC}
    )


def test_shelltool_refuses_without_runner():
    r = ShellTool().execute(command="echo hi")
    assert r.ok is False
    assert "sandbox not configured" in r.error
    assert r.metadata.get("sandboxed") is False
    assert r.output is None


def test_shelltool_executes_through_docker_runner(monkeypatch):
    rec = _Recorder(result=_completed(returncode=0, stdout="ran\n", stderr=""))
    monkeypatch.setattr(subprocess, "run", rec)

    tool = ShellTool.with_docker_sandbox(env={"PATH": "/usr/bin"})
    res = tool.execute(command="echo hi")

    assert res.ok is True
    assert res.metadata["sandboxed"] is True
    assert res.output["stdout"] == "ran\n"
    # It really went through the hardened docker invocation.
    argv = rec.calls[0]["argv"]
    assert argv[:2] == ["docker", "run"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--rm" in argv


def test_shelltool_reports_sandbox_failure(monkeypatch):
    rec = _Recorder(result=_completed(returncode=1, stdout="", stderr="nope"))
    monkeypatch.setattr(subprocess, "run", rec)
    res = ShellTool.with_docker_sandbox(env={}).execute(command="false")
    assert res.ok is False and res.error == "nope"


def test_docker_available_does_not_run_container(monkeypatch):
    monkeypatch.setattr("runtime.sandbox.docker.shutil.which", lambda _b: None)
    assert DockerSandboxRunner(env={}).docker_available() is False
    monkeypatch.setattr("runtime.sandbox.docker.shutil.which", lambda _b: "/usr/bin/docker")
    assert DockerSandboxRunner(env={}).docker_available() is True


def test_module_import_does_not_require_docker():
    # Importing the package/module must not need Docker present or installed.
    import importlib

    mod = importlib.import_module("runtime.sandbox")
    assert isinstance(mod.DockerSandboxRunner, type)
    # Constructing a runner never shells out either.
    DockerSandboxRunner(env={})
