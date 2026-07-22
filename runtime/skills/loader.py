"""Parse a ``SKILL.md`` (Agent Skills open standard) into a :class:`Skill`.

Format (open standard):

    ---
    name: define-success-criteria
    description: Define one measurable success criterion before executing.
    triggers: [plan, success criteria, confidence gate]
    reviewed: true
    source: in-repo
    ---
    <markdown instructions...>

The frontmatter is a ``---``-fenced YAML block at the very top of the file; the
remainder is the markdown instruction body. Required fields are ``name`` and
``description``. Anything malformed (missing/broken fence, non-mapping YAML,
invalid YAML, missing required field) raises :class:`SkillError` with a clear,
path-qualified message — the loader NEVER crashes the caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml
from pydantic import ValidationError

from .models import Skill, SkillError

_FENCE = "---"


def _split_frontmatter(text: str, where: str) -> tuple[str, str]:
    """Return ``(yaml_text, body_text)`` from a ``---``-fenced document.

    Raises :class:`SkillError` if the opening/closing fence is missing.
    """
    # Normalise leading blank lines / BOM but require the fence to lead.
    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    # Find the opening fence (first non-blank line must be the fence).
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != _FENCE:
        raise SkillError(
            f"{where}: missing opening '---' YAML frontmatter fence "
            "(a SKILL.md must start with a '---' fenced YAML block)"
        )
    open_idx = idx
    # Find the closing fence.
    close_idx = None
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip() == _FENCE:
            close_idx = j
            break
    if close_idx is None:
        raise SkillError(f"{where}: unterminated YAML frontmatter (no closing '---')")
    yaml_text = "\n".join(lines[open_idx + 1 : close_idx])
    body_text = "\n".join(lines[close_idx + 1 :]).strip()
    return yaml_text, body_text


def parse_skill(text: str, *, source_path: Union[str, Path, None] = None) -> Skill:
    """Parse SKILL.md ``text`` into a validated :class:`Skill`.

    ``source_path`` is used only for error messages / provenance. Raises
    :class:`SkillError` (never a bare YAML/validation error) on any problem.
    """
    where = str(source_path) if source_path is not None else "<skill>"
    yaml_text, body = _split_frontmatter(text, where)

    try:
        meta = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SkillError(f"{where}: invalid YAML frontmatter: {exc}") from exc

    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SkillError(
            f"{where}: frontmatter must be a YAML mapping, got {type(meta).__name__}"
        )

    # Normalise a few accepted aliases so authors can use natural key names.
    data = dict(meta)
    if "when_to_use" not in data and "when-to-use" in data:
        data["when_to_use"] = data.pop("when-to-use")
    # `triggers` may be given as a comma-separated string; coerce to a list.
    trig = data.get("triggers")
    if isinstance(trig, str):
        data["triggers"] = [t.strip() for t in trig.split(",") if t.strip()]

    data["instructions"] = body
    if source_path is not None:
        data["path"] = str(Path(source_path).resolve())

    try:
        return Skill(**data)
    except ValidationError as exc:
        # Collapse to a single readable line; do not leak a stack trace.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise SkillError(f"{where}: invalid skill metadata: {problems}") from exc


def load_skill(path: Union[str, Path]) -> Skill:
    """Read + parse a ``SKILL.md`` file. Raises :class:`SkillError` on any error."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"{p}: cannot read skill file: {exc}") from exc
    return parse_skill(text, source_path=p)
