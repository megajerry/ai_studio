"""Tool abstraction + result type (CLAUDE.md invariants 2 & 3).

A **tool is the only thing that performs a side effect.** Agents never touch the
host directly — they ask the enforced invocation path (:mod:`runtime.enforce`)
to run a tool by name, and the policy engine gates the call first.

Two rules every tool upholds:

1. **Declare capabilities.** ``required_capabilities`` is the static superset a
   tool may need; :meth:`Tool.capabilities_for` narrows it per call so the policy
   engine can enforce least privilege (a *read* costs only ``fs.read``, even on a
   tool that can also write/delete).
2. **Read secrets from the environment, never from the agent.** A tool that needs
   a credential reads it from ``os.environ`` / the secret store itself and acts on
   the agent's behalf (CLAUDE.md invariant 5, ADR-0011). Secrets must never be
   passed in as ``execute`` kwargs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from ..capabilities import Capability


class ToolResult(BaseModel):
    """Outcome of a tool execution.

    ``ok`` is the tool-level success flag (a *handled* failure such as "file not
    found" returns ``ok=False`` with ``error`` set, rather than raising).
    ``output`` and ``metadata`` are JSON-serializable so they can ride in the
    event log.
    """

    ok: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Tool(ABC):
    """Base class for every side-effecting tool.

    Subclasses set ``name`` and ``required_capabilities`` (the static superset)
    and implement :meth:`execute`. Override :meth:`capabilities_for` when the
    capabilities a call needs depend on its arguments (e.g. filesystem
    read/write/delete).
    """

    #: Stable identifier used to look the tool up in the registry.
    name: str = ""
    #: Superset of capabilities this tool may require across all its operations.
    required_capabilities: frozenset[Capability] = frozenset()

    def capabilities_for(self, **kwargs: Any) -> frozenset[Capability]:
        """Capabilities this specific call requires (defaults to the superset).

        Narrowing per call is what lets the policy engine enforce least
        privilege on multi-operation tools.
        """
        return self.required_capabilities

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Perform the side effect. Only ever reached after the policy engine
        has ALLOWed the call (see :mod:`runtime.enforce`)."""
        raise NotImplementedError
