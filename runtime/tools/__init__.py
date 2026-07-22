"""Tool registry — look tools up by name (CLAUDE.md invariants 2 & 3).

The enforced invocation path (:mod:`runtime.enforce`) resolves a tool from this
registry by name, so agents reference tools by a string identifier and never
hold a direct handle to a side-effecting object.

A :class:`ToolRegistry` instance is explicit (pass one around / inject in tests);
:data:`default_registry` is a process-wide convenience registry that the bundled
reference tools are *not* auto-registered into — the caller registers the tools
(and their configured roots/sandboxes) it wants, keeping construction explicit.
"""

from __future__ import annotations

from .base import Tool, ToolResult
from .filesystem import FilesystemTool
from .shell import SandboxRunner, ShellTool


class ToolRegistry:
    """A name → :class:`Tool` map."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        """Register a tool under its ``name``. Rejects blank or duplicate names."""
        if not tool.name:
            raise ValueError("tool must declare a non-empty name")
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        """Return the registered tool, or ``None`` if unknown."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


#: Process-wide convenience registry (empty until the caller registers tools).
default_registry = ToolRegistry()


__all__ = [
    "FilesystemTool",
    "SandboxRunner",
    "ShellTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "default_registry",
]
