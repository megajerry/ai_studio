"""Pluggable verify-checker registry — the domain-check seam (ADR-0014, ADR-0003).

The Verifier is the independent verify→commit gate: it decides ``done`` on
**evidence it observes itself**, never the Executor's claim (ADR-0014). The *shape*
of that evidence check is horizontal for text artifacts (a success marker must be
present), but a **vertical** needs its own domain check — a video channel wants a
``video_audit`` (duration, loudness, captions present), a data pipeline wants a
row-count / schema check, and so on.

This module makes the check **pluggable without touching the Verifier**:

- a :class:`Checker` protocol — ``check(conn, task, artifact_ref, require) ->``
  :class:`CheckResult` — gathers evidence and returns a FACTS-based verdict;
- an :class:`ArtifactRef` bundling everything a checker needs to gather evidence
  (the artifact path + the policy-gated read seam + the Executor's un-trusted
  result);
- a :class:`CheckerRegistry` keyed by criterion ``check`` name, with a default
  :func:`marker_check` (the current marker-in-file logic) pre-registered on
  :data:`DEFAULT_REGISTRY`.

A criterion is structured ``{"check": name, "require": ...}``; a bare marker
string is back-compat sugar for ``{"check": "marker", "require": marker}`` (see
:func:`resolve_criterion`). The Verifier dispatches to the registered checker and
decides on the returned FACTS — so a vertical injects a domain check while reusing
the shared verify→commit gate, learning, retro, reviewer, and telemetry unchanged.

Everything here only READS (via the policy-gated ``verifier`` role, ``fs.read``):
a checker can never mutate the work it is judging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..models import Task
from ..policy import PolicyConfig
from ..tools import ToolRegistry


class CheckResult(BaseModel):
    """A checker's evidence-based verdict.

    ``passed`` is the decision; ``facts`` are the concrete pieces of evidence the
    checker OBSERVED (never model claims) — recorded for traceability (ADR-0014);
    ``reason`` is a one-line justification citing that evidence.
    """

    passed: bool
    facts: dict = Field(default_factory=dict)
    reason: str = ""


@dataclass
class ArtifactRef:
    """Everything a checker needs to gather evidence about the produced artifact.

    Bundles the artifact location with the **policy-gated read seam** (the same
    ``filesystem`` tool/root the Executor wrote to) so a checker re-reads the REAL
    artifact through ``invoke(role="verifier", …)`` — least privilege, read-only.
    ``result`` carries the Executor's output, available but explicitly NOT trusted
    as evidence (ADR-0014).
    """

    registry: ToolRegistry
    path: Optional[str] = None
    config: Optional[PolicyConfig] = None
    sink: EventSink = field(default_factory=NullEventSink)
    result: Any = None

    def read_text(self, task: Task, path: Optional[str] = None):
        """Policy-gated read of ``path`` (default: :attr:`path`) as the ``verifier``.

        Returns the raw ``InvokeResult`` so a checker can inspect the observed
        status + contents itself (evidence over claims). Read-only by construction:
        the ``verifier`` role is granted only ``fs.read``.
        """
        return invoke(
            role="verifier",
            tool_name="filesystem",
            registry=self.registry,
            config=self.config,
            events=self.sink,
            workstream=task.workstream,
            task_id=task.id,
            op="read",
            path=path or self.path,
        )


#: A checker: gather evidence for ``task``/``artifact_ref`` and judge ``require``.
Checker = Callable[[Any, Task, ArtifactRef, Any], CheckResult]


class UnknownChecker(KeyError):
    """Raised when a criterion names a ``check`` with no registered checker."""

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(
            f"unknown verify checker {name!r}; registered checkers: {known or '(none)'}"
        )


class CheckerRegistry:
    """A name→:class:`Checker` map the Verifier dispatches on (keyed by criterion)."""

    def __init__(self) -> None:
        self._by_name: dict[str, Checker] = {}

    def register(self, name: str, checker: Checker) -> None:
        """Register (or replace) the checker for criterion ``name``."""
        self._by_name[name] = checker

    def get(self, name: str) -> Optional[Checker]:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def run(
        self, name: str, conn: Any, task: Task, artifact_ref: ArtifactRef, require: Any
    ) -> CheckResult:
        """Dispatch to the checker registered as ``name``; error clearly if none."""
        checker = self._by_name.get(name)
        if checker is None:
            raise UnknownChecker(name, self.names())
        return checker(conn, task, artifact_ref, require)


def marker_check(
    conn: Any, task: Task, artifact_ref: ArtifactRef, require: Any
) -> CheckResult:
    """Default checker: re-read the artifact and confirm it contains the marker.

    The horizontal evidence gate (ADR-0014): the decision rests on the artifact's
    REAL contents observed here, never on ``result.ok`` (the Executor's claim). A
    result claiming success whose artifact lacks the marker still FAILS. ``require``
    is the marker string (a structured ``{"marker": ...}`` / ``{"value": ...}`` is
    also accepted).
    """
    marker = _marker_from_require(require)
    if not artifact_ref.path:
        return CheckResult(
            passed=False,
            facts={"artifact": None},
            reason="no artifact produced by executor",
        )
    if not marker:
        return CheckResult(
            passed=False, facts={"marker": None}, reason="no success marker defined"
        )

    read = artifact_ref.read_text(task)
    if read.status is not InvokeStatus.EXECUTED or not (read.result and read.result.ok):
        return CheckResult(
            passed=False,
            facts={"read_status": read.status.value},
            reason=f"could not read artifact ({read.status.value})",
        )

    content = read.result.output or ""
    facts = {"marker": marker, "artifact_bytes": len(content), "read_status": "executed"}
    if marker in content:
        return CheckResult(
            passed=True, facts=facts, reason=f"artifact contains marker {marker!r}"
        )
    return CheckResult(
        passed=False, facts=facts, reason=f"marker {marker!r} not found in artifact"
    )


def _marker_from_require(require: Any) -> str:
    """Coerce ``require`` to the marker string (accepts a bare str or a dict)."""
    if isinstance(require, str):
        return require
    if isinstance(require, dict):
        return str(require.get("marker") or require.get("value") or "")
    return ""


def resolve_criterion(payload: dict, *, fallback_marker: str) -> tuple[str, Any]:
    """Resolve a task payload to a ``(check_name, require)`` pair for dispatch.

    Structured criterion (preferred) lives at ``payload["check"]`` as either a dict
    ``{"check": name, "require": ...}`` or a bare check-name string. With neither
    present the criterion is the back-compat MARKER check on ``fallback_marker``
    (``payload["marker"]`` / the executor's marker) — so every existing task keeps
    verifying exactly as before.
    """
    spec = (payload or {}).get("check")
    if isinstance(spec, dict):
        name = str(spec.get("check") or "marker")
        require = spec.get("require")
        if name == "marker" and not require:
            require = fallback_marker
        return name, require
    if isinstance(spec, str) and spec.strip():
        name = spec.strip()
        require = payload.get("require", fallback_marker if name == "marker" else None)
        return name, require
    return "marker", fallback_marker


def default_registry() -> CheckerRegistry:
    """A fresh registry with the horizontal ``marker`` checker pre-registered.

    Verticals build on this (register their own domain checks) or construct their
    own :class:`CheckerRegistry`.
    """
    reg = CheckerRegistry()
    reg.register("marker", marker_check)
    return reg


#: The process-wide default registry the Verifier uses unless given another.
DEFAULT_REGISTRY = default_registry()
