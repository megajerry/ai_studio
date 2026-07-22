"""Shell tool — declares ``shell.exec`` (🔴) and refuses to run unsandboxed.

CLAUDE.md invariant 2 + architecture §8: **no agent shells directly; everything
runs in a Docker sandbox.** That sandbox is not built yet, so this tool must
never shell out on the host.

Two independent guards make that safe:

1. **Tier gate.** ``shell.exec`` is 🔴, so the policy engine returns
   NEEDS_APPROVAL and :func:`runtime.enforce.invoke` never even calls
   :meth:`execute` without a resolved approval.
2. **Defense in depth.** Even if :meth:`execute` is reached, it refuses unless a
   :class:`SandboxRunner` has been injected. With no sandbox configured it
   returns a clear ``ok=False`` "sandbox not configured" result — it does not run
   anything on the host.

The Docker sandbox is the extension point: implement :class:`SandboxRunner`
(run the command inside a container / VM, no host access) and pass it to
``ShellTool(sandbox=...)``. Nothing else about the call path changes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..capabilities import Capability
from .base import Tool, ToolResult


@runtime_checkable
class SandboxRunner(Protocol):
    """A confined command runner (future Docker/VM sandbox).

    An implementation runs ``command`` fully isolated from the host and returns
    ``(exit_code, stdout, stderr)``. Until one exists, :class:`ShellTool` refuses.
    """

    def run(self, command: str, **kwargs) -> tuple[int, str, str]:
        ...


class ShellTool(Tool):
    """Run a shell command — but only inside a configured sandbox."""

    name = "shell"
    required_capabilities = frozenset({Capability.SHELL_EXEC})

    def __init__(self, sandbox: SandboxRunner | None = None) -> None:
        #: Injected later when the Docker sandbox lands; ``None`` = refuse.
        self.sandbox = sandbox

    @classmethod
    def with_docker_sandbox(cls, **runner_kwargs) -> "ShellTool":
        """Build a :class:`ShellTool` backed by a :class:`DockerSandboxRunner`.

        Convenience wiring for the concrete Docker sandbox (config resolves from
        ``SANDBOX_*`` env / ``runner_kwargs``). Docker is imported **lazily** here
        and is not required at module import; a container is only ever launched
        when :meth:`execute` runs — which, because ``shell.exec`` is 🔴, only
        happens after the policy engine resolves a human approval. The tier gate
        and the refuse-without-sandbox guard are both unchanged.
        """
        from ..sandbox import DockerSandboxRunner

        return cls(sandbox=DockerSandboxRunner(**runner_kwargs))

    def execute(self, **kwargs) -> ToolResult:
        command = kwargs.get("command")
        if not command:
            return ToolResult(ok=False, error="'command' is required")

        if self.sandbox is None:
            # Never shell out on the host. This is the second line of defense
            # behind the 🔴 tier gate in the policy engine.
            return ToolResult(
                ok=False,
                error=(
                    "sandbox not configured: ShellTool refuses to run commands "
                    "on the host. Inject a SandboxRunner (Docker) to enable."
                ),
                metadata={"sandboxed": False},
            )

        run_kwargs = {k: v for k, v in kwargs.items() if k != "command"}
        exit_code, stdout, stderr = self.sandbox.run(command, **run_kwargs)
        return ToolResult(
            ok=exit_code == 0,
            output={"stdout": stdout, "exit_code": exit_code},
            error=stderr or None,
            metadata={"sandboxed": True},
        )
