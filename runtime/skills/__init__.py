"""Skills layer — Agent Skills open standard for roles (ADR-0008, architecture §14).

A role is ``prompt + skills + tools``. This package is the **skills** part: a
``SKILL.md`` (YAML frontmatter + markdown instructions + optional sibling
resources) is a portable, reviewable capability package, loaded **on demand** and
injected into a role's prompt only when relevant.

Pipeline:

    SkillRegistry.discover(root)      # parse every SKILL.md under the skills root
        .select(role|task_type|query) # pick only the RELEVANT ones, capped
    compose_prompt(base, selected)    # inject the REVIEWED ones into the prompt

Two safety invariants (treat skills like code):

- **Review before use** — only ``reviewed`` skills are injected by default;
  unreviewed ones are skipped + logged (``allow_unreviewed=True`` to override).
- **No auto-execution** — a skill contributes INSTRUCTIONS only. Loading or
  injecting a skill never runs its resources/scripts; any execution still goes
  through the policy-gated tool layer (:func:`runtime.enforce.invoke`).

See ``runtime/skills.md`` and ``skills/README.md``.
"""

from __future__ import annotations

from .inject import (
    Composition,
    compose,
    compose_prompt,
    emit_skill_applied,
    filter_injectable,
)
from .loader import load_skill, parse_skill
from .models import Skill, SkillError
from .registry import DEFAULT_SELECT_LIMIT, SkillRegistry, default_root

__all__ = [
    "Skill",
    "SkillError",
    "load_skill",
    "parse_skill",
    "SkillRegistry",
    "default_root",
    "DEFAULT_SELECT_LIMIT",
    "compose",
    "compose_prompt",
    "emit_skill_applied",
    "filter_injectable",
    "Composition",
]
