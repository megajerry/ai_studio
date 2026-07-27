"""Classify test/demo queue noise so stakeholder views stay real-only.

Live pytest suites enqueue into ephemeral workstreams (``skilllc-<hex>``,
``test-spk-…``, ``gw-<hex>-other``, …) and demo types (``work.demo``). Those rows
must not dominate the dashboard or the ``status`` feed. The queue itself is
untouched — this is a **read filter** only.
"""

from __future__ import annotations

import re

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


def real_task_sql(alias: str = "") -> str:
    """SQL boolean: row is stakeholder-visible.

    ``alias`` is an optional table alias prefix (``\"t\"`` → ``t.workstream``).
    Keep in sync with :func:`is_noise_workstream` / :func:`is_noise_task_type`.
    """
    p = f"{alias}." if alias else ""
    return f"""(
  {p}workstream IS NOT NULL
  AND {p}workstream <> ''
  AND lower({p}workstream) <> 'test'
  AND {p}workstream !~* '^(test[-_]|test-spk-|skilllc-|curate-|pm-research-|traj-|grnd-|lesson-|boot-|glob-|race-|gw-|cap-|fail-|worker-|quality-|dec-|spk-|gate-|cleanup-)'
  AND {p}workstream !~* '(^|[_-])[0-9a-f]{{6,}}(-other)?$'
  AND {p}type <> ALL(ARRAY['work.demo','work.probe','work.stall','work.stuck','work.real','t'])
)"""


#: Default form (no table alias) for simple ``FROM tasks`` scans.
REAL_TASK_SQL = real_task_sql()
