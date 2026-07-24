"""Coding tool — dispatch a coding worker (opencode) INSIDE the sandbox.

Architecture §14: **opencode is one replaceable Worker, not the runtime.** The
Builder never knows which coding agent runs; it only knows "Need Prototype," and
the runtime dispatches a Worker. This tool is that dispatch seam: given a
prototype spec (a goal + a workspace ref), it runs a coding CLI **inside the
Docker sandbox** (never on the host) and returns a :class:`ToolResult` carrying
the produced-files ref and the worker's exit status.

Same two independent guards as :class:`~runtime.tools.shell.ShellTool`:

1. **Tier gate.** ``code.run`` is 🔴, so :func:`runtime.enforce.invoke` returns
   NEEDS_APPROVAL and never reaches :meth:`execute` without a resolved human
   approval (ADR-0006). Running agent-authored code is the same escape-the-sandbox
   risk class as ``shell.exec``.
2. **Defense in depth.** Even if :meth:`execute` is reached, it refuses unless a
   :class:`~runtime.tools.shell.SandboxRunner` has been injected — it NEVER runs
   the coding worker on the host. With no sandbox it returns a clear ``ok=False``
   "sandbox not configured" result.

**opencode is swappable.** The worker command is configurable via
``CODING_WORKER_CMD`` (default ``opencode``); swapping to Claude/Gemini/any CLI
coding agent is a config change, not a code change — the studio's brain
(orchestration, policy, memory, evaluation) stays stable.

**No secrets leak.** The tool reads no secrets itself; any credential the worker
needs is forwarded only via the sandbox's explicit ``allowed_env`` allowlist
(the values are handed to the ``docker`` client by name, never in the argv —
CLAUDE.md invariant 5, ADR-0011). Everything else — every secret — stays out.
"""

from __future__ import annotations

import os
import shlex

from ..capabilities import Capability
from .base import Tool, ToolResult
from .shell import SandboxRunner

#: Env var that overrides the coding worker CLI (keeps opencode swappable).
CODING_WORKER_CMD_ENV = "CODING_WORKER_CMD"
#: Default coding worker (architecture §14: the first replaceable "employee").
DEFAULT_CODING_WORKER_CMD = "opencode"

#: Per-worker command SHAPE, keyed by the worker binary's basename. Different
#: coding CLIs take the goal differently — opencode uses ``run <goal>``; Cursor's
#: agent harness uses ``-p <goal> --output-format json`` (its non-interactive
#: form; there is NO raw HTTP inference endpoint, only this CLI). ``{goal}`` is
#: substituted with the SHELL-QUOTED goal so it is always a single argument that
#: cannot break out of the invocation. A worker not listed here falls back to
#: :data:`_DEFAULT_TEMPLATE` (the opencode ``run`` convention), so adding a new
#: coding CLI is a one-line config/mapping change, not a code rewrite.
WORKER_COMMAND_TEMPLATES: dict[str, str] = {
    "opencode": "{cmd} run {goal}",
    "cursor-agent": "{cmd} -p {goal} --output-format json",
}
#: Shape used for any worker not in :data:`WORKER_COMMAND_TEMPLATES`.
_DEFAULT_TEMPLATE = "{cmd} run {goal}"


class CodingTool(Tool):
    """Run a coding worker on a prototype spec — but only inside a sandbox."""

    name = "coding"
    required_capabilities = frozenset({Capability.CODE_RUN})

    def __init__(
        self,
        sandbox: SandboxRunner | None = None,
        *,
        worker_cmd: str | None = None,
    ) -> None:
        #: Injected sandbox (Docker/VM). ``None`` = refuse (never run on host).
        self.sandbox = sandbox
        #: Resolved coding CLI (arg > ``CODING_WORKER_CMD`` env > ``opencode``).
        self.worker_cmd = (
            worker_cmd
            or os.environ.get(CODING_WORKER_CMD_ENV)
            or DEFAULT_CODING_WORKER_CMD
        )

    @classmethod
    def with_docker_sandbox(cls, *, worker_cmd: str | None = None, **runner_kwargs) -> "CodingTool":
        """Build a :class:`CodingTool` backed by a :class:`DockerSandboxRunner`.

        Docker is imported **lazily** here and is not required at module import;
        a container is only ever launched when :meth:`execute` runs — which,
        because ``code.run`` is 🔴, only happens after the policy engine resolves a
        human approval. ``runner_kwargs`` (e.g. ``workdir=`` the scratch workspace,
        ``allowed_env=[...]``) configure the sandbox exactly as for ShellTool; the
        env allowlist is the ONLY channel any credential reaches the worker.
        """
        from ..sandbox import DockerSandboxRunner

        return cls(sandbox=DockerSandboxRunner(**runner_kwargs), worker_cmd=worker_cmd)

    def build_command(self, goal: str) -> str:
        """Build the shell command that runs the coding worker on ``goal``.

        Pure + unit-tested (no Docker). The goal is shell-quoted so it is passed as
        a single argument to the worker and can never break out of the invocation.
        The command SHAPE is per-worker (:data:`WORKER_COMMAND_TEMPLATES`, keyed by
        the worker binary's basename): opencode -> ``<cmd> run <goal>``;
        ``cursor-agent`` -> ``<cmd> -p <goal> --output-format json``. Unknown
        workers fall back to the opencode ``run`` convention. The command runs in
        the container's workdir (the bind-mounted scratch workspace at
        ``/workspace``), so files the worker writes land there.
        """
        template = WORKER_COMMAND_TEMPLATES.get(
            os.path.basename(self.worker_cmd), _DEFAULT_TEMPLATE
        )
        return template.format(cmd=self.worker_cmd, goal=shlex.quote(goal))

    def execute(self, **kwargs) -> ToolResult:
        goal = kwargs.get("goal")
        if not goal:
            return ToolResult(ok=False, error="'goal' is required")
        # Workspace ref is echoed back as the produced-files ref; the actual
        # bind mount is configured on the sandbox (SANDBOX_WORKDIR / workdir=).
        workspace = kwargs.get("workspace")

        if self.sandbox is None:
            # Never run the coding worker on the host. Second line of defense
            # behind the 🔴 tier gate in the policy engine.
            return ToolResult(
                ok=False,
                error=(
                    "sandbox not configured: CodingTool refuses to run the coding "
                    "worker on the host. Inject a SandboxRunner (Docker) to enable."
                ),
                metadata={"sandboxed": False, "worker_cmd": self.worker_cmd},
            )

        command = self.build_command(goal)
        exit_code, stdout, stderr = self.sandbox.run(command)
        return ToolResult(
            ok=exit_code == 0,
            output={
                "stdout": stdout,
                "exit_code": exit_code,
                # Produced files live in the sandbox's bind-mounted workspace.
                "produced_files": workspace,
            },
            error=stderr or None,
            metadata={
                "sandboxed": True,
                "worker_cmd": self.worker_cmd,
                "workspace": workspace,
            },
        )
