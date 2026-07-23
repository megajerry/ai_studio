"""Built-in domain verify-checkers a workstream config can enable by name.

The Verifier dispatches a structured criterion ``{check, require}`` to a
:class:`runtime.roles.checkers.Checker` (ADR-0014). The horizontal ``marker``
check is always available; a **vertical** needs its own domain check. Rather than
have a vertical ship checker *code* in its config (config is data, not code —
ADR-0011), the platform ships a small registry of built-in domain checkers here
and a workstream ENABLES the ones it needs **by name** in
``workstreams/<name>/config.yaml`` (``checkers: [video_audit]``).

:data:`BUILTIN_CHECKERS` is that name→checker map. Adding a new domain check is a
small, reviewed platform contribution (register it here); any workstream then
turns it on with one config line — no Verifier change, no per-vertical role code.
:func:`register_checker` lets a host wire additional checkers at startup.

Every checker judges on **evidence it observes itself** (re-reads the real
artifact through the policy-gated ``verifier`` read seam), never the author's
claim — a false "done" whose artifact fails the check still FAILS.
"""

from __future__ import annotations

import re
from typing import Any

from ..enforce import InvokeStatus
from ..roles.checkers import ArtifactRef, Checker, CheckResult, marker_check
from ..models import Task


def video_audit(conn: Any, task: Task, ref: ArtifactRef, require: Any) -> CheckResult:
    """Domain check for a video vertical: audit the produced clip's real facts.

    Re-reads the artifact and judges on OBSERVED facts — duration + captions —
    never the author's claim (ADR-0014). ``require`` is a mapping:

    - ``min_seconds`` (int) — the clip must be at least this long;
    - ``captions`` (bool) — captions must be present when true.

    The artifact is expected to record ``duration_seconds: <n>`` and
    ``captions: yes|no`` (the shape the video Executor/render step emits). A clip
    that claims success but is too short / missing captions FAILS.
    """
    read = ref.read_text(task)
    content = (
        (read.result.output or "")
        if (read.status is InvokeStatus.EXECUTED and read.result and read.result.ok)
        else ""
    )
    m = re.search(r"duration_seconds:\s*(\d+)", content)
    seconds = int(m.group(1)) if m else 0
    has_captions = "captions: yes" in content

    require = require or {}
    min_seconds = int(require.get("min_seconds", 0))
    need_captions = bool(require.get("captions", False))

    facts = {
        "duration_seconds": seconds,
        "captions": has_captions,
        "read_status": read.status.value,
    }
    if not content:
        return CheckResult(
            passed=False, facts=facts, reason="could not read the rendered clip"
        )
    ok = seconds >= min_seconds and (has_captions or not need_captions)
    reason = (
        f"clip {seconds}s >= {min_seconds}s and captions ok"
        if ok
        else (
            f"failed audit (duration {seconds}s < {min_seconds}s"
            + ("" if has_captions or not need_captions else " or captions missing")
            + ")"
        )
    )
    return CheckResult(passed=ok, facts=facts, reason=reason)


#: name → :class:`Checker`. A workstream config's ``checkers:`` names a subset of
#: these; the horizontal ``marker`` gate is always registered on top separately
#: (:func:`runtime.roles.checkers.default_registry`), so it need not be listed.
BUILTIN_CHECKERS: dict[str, Checker] = {
    "marker": marker_check,
    "video_audit": video_audit,
}


def register_checker(name: str, checker: Checker) -> None:
    """Register (or replace) a built-in domain checker a config may enable by name."""
    BUILTIN_CHECKERS[name] = checker
