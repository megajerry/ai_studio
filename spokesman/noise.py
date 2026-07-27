"""Classify test/demo queue noise so stakeholder views stay real-only.

Primary signal: ``payload.traffic = 'test'`` (:mod:`runtime.traffic`). Secondary:
ephemeral pytest workstreams / demo types. Never match on goal text. Read filter
only — the queue is untouched.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

#: Workstreams that are known fixture/demo sandboxes (exact match).
_NOISE_WORKSTREAMS_EXACT = frozenset({"test"})

#: Prefixes used by live DB tests / throwaway verification.
_NOISE_WORKSTREAM_PREFIXES = (
    "test-",
    "test_",
    "test-spk-",
    "skilllc-",
    "curate-",
    "pm-research-",
    "traj-",
    "grnd-",
    "lesson-",
    "boot-",
    "glob-",
    "race-",
    "gw-",          # gateway verification throwaways
    "cap-",
    "fail-",
    "worker-",
    "quality-",
    "dec-",
    "spk-",
    "gate-",
    "cleanup-",
)

#: Task types reserved for fixtures / demos — never stakeholder work.
_NOISE_TASK_TYPES = frozenset({
    "work.demo",
    "work.probe",
    "work.stall",
    "work.stuck",
    "work.real",  # fixture name, not a real vertical type
    "t",
})

#: ``skilllc-ab12…``, ``foo-1a2b3c4d``, ``gw-….``-style disposable names.
_HEX_SUFFIX_WS = re.compile(
    r"(?:^|[_-])[0-9a-f]{6,}(?:-other)?$",
    re.IGNORECASE,
)


def is_noise_workstream(workstream: str | None) -> bool:
    """True when ``workstream`` is a test/demo sandbox, not a real vertical."""
    ws = (workstream or "").strip()
    if not ws:
        return True
    if ws in _NOISE_WORKSTREAMS_EXACT:
        return True
    lower = ws.lower()
    if any(lower.startswith(p) for p in _NOISE_WORKSTREAM_PREFIXES):
        return True
    if _HEX_SUFFIX_WS.search(lower):
        return True
    return False


def is_noise_task_type(task_type: str | None) -> bool:
    """True when ``task_type`` is a fixture/demo type."""
    return (task_type or "").strip() in _NOISE_TASK_TYPES


def is_noise_task(
    *,
    workstream: str | None = None,
    task_type: str | None = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> bool:
    """True when a task must be hidden from stakeholder views / prod cleanups."""
    if payload is not None and str(payload.get("traffic") or "").lower() == "test":
        return True
    if is_noise_workstream(workstream):
        return True
    if is_noise_task_type(task_type):
        return True
    return False


def real_task_sql(alias: str = "") -> str:
    """SQL boolean: row is stakeholder-visible.

    ``alias`` is an optional table alias prefix (``\"t\"`` → ``t.workstream``).
    Prefer ``payload.traffic``; fall back to workstream/type heuristics for
    legacy rows enqueued before traffic tagging.
    """
    p = f"{alias}." if alias else ""
    return f"""(
  coalesce({p}payload->>'traffic', 'prod') <> 'test'
  AND {p}workstream IS NOT NULL
  AND {p}workstream <> ''
  AND lower({p}workstream) <> 'test'
  AND {p}workstream !~* '^(test[-_]|test-spk-|skilllc-|curate-|pm-research-|traj-|grnd-|lesson-|boot-|glob-|race-|gw-|cap-|fail-|worker-|quality-|dec-|spk-|gate-|cleanup-)'
  AND {p}workstream !~* '(^|[_-])[0-9a-f]{{6,}}(-other)?$'
  AND {p}type <> ALL(ARRAY['work.demo','work.probe','work.stall','work.stuck','work.real','t'])
)"""


#: Default form (no table alias) for simple ``FROM tasks`` scans.
REAL_TASK_SQL = real_task_sql()
