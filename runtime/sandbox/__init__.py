"""Sandbox runners — confined command execution for the 🔴 ``shell`` tool.

CLAUDE.md invariant 2 + architecture §8 (zero trust): **no agent shells directly;
everything runs in a Docker sandbox.** :class:`~runtime.tools.shell.ShellTool`
declares the extension point (the ``SandboxRunner`` protocol); this package
provides the concrete Docker implementation.

Nothing here imports or requires Docker at import time — a runner only shells out
to the ``docker`` CLI when :meth:`DockerSandboxRunner.run` is actually called.
"""

from __future__ import annotations

from .docker import (
    CONTAINER_WORKDIR,
    TIMEOUT_EXIT_CODE,
    DockerSandboxRunner,
    SandboxConfigError,
)

__all__ = [
    "DockerSandboxRunner",
    "SandboxConfigError",
    "CONTAINER_WORKDIR",
    "TIMEOUT_EXIT_CODE",
]
