"""Skill registry — discover ``SKILL.md`` files + select the relevant ones.

Discovery walks a *skills root* (default: the repo-level ``skills/`` directory;
overridable via ``$AI_STUDIO_SKILLS_DIR`` or the ``root=`` argument) for
``SKILL.md`` files, parses each, and indexes by ``name``. A malformed skill is
**logged and skipped** — one bad file never breaks discovery (ADR-0008: fail
gracefully).

:meth:`SkillRegistry.select` returns only the skills RELEVANT to a query (role /
task_type / free text), capped to ``limit`` — the on-demand loading discipline
(ADR-0013: load only what's relevant, never everything).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union

from .loader import load_skill
from .models import Skill, SkillError

logger = logging.getLogger("runtime.skills")

_ENV_SKILLS_DIR = "AI_STUDIO_SKILLS_DIR"
#: Repo root is three parents up from this file (runtime/skills/registry.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_ROOT = _REPO_ROOT / "skills"

#: Default cap on how many skills are ever selected for one prompt (context
#: discipline — a prompt should never be flooded with capability packages).
DEFAULT_SELECT_LIMIT = 3


def default_root() -> Path:
    """The skills root: ``$AI_STUDIO_SKILLS_DIR`` if set, else repo ``skills/``."""
    env = os.environ.get(_ENV_SKILLS_DIR)
    return Path(env).expanduser() if env else _DEFAULT_ROOT


class SkillRegistry:
    """An in-memory index of parsed skills, discovered from a skills root."""

    def __init__(self, skills: Optional[list[Skill]] = None) -> None:
        self._by_name: dict[str, Skill] = {}
        for s in skills or []:
            self._by_name[s.name] = s

    # --- construction -------------------------------------------------------

    @classmethod
    def discover(cls, root: Union[str, Path, None] = None) -> "SkillRegistry":
        """Build a registry by scanning ``root`` (default :func:`default_root`).

        Every ``SKILL.md`` under ``root`` is parsed; malformed ones are logged
        and skipped. A missing root yields an empty registry (not an error).
        """
        base = Path(root).expanduser() if root is not None else default_root()
        reg = cls()
        if not base.exists():
            logger.info("skills root %s does not exist; no skills loaded", base)
            return reg
        for skill_file in sorted(base.rglob("SKILL.md")):
            try:
                skill = load_skill(skill_file)
            except SkillError as exc:
                logger.warning("skipping malformed skill: %s", exc)
                continue
            if skill.name in reg._by_name:
                logger.warning(
                    "duplicate skill name %r (%s) shadows %s; keeping the first",
                    skill.name,
                    skill_file,
                    reg._by_name[skill.name].path,
                )
                continue
            reg._by_name[skill.name] = skill
        return reg

    # --- access -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._by_name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def all(self) -> list[Skill]:
        return [self._by_name[n] for n in self.names()]

    def get(self, name: str) -> Optional[Skill]:
        return self._by_name.get(name)

    def register(self, skill: Skill) -> None:
        self._by_name[skill.name] = skill

    # --- selection ----------------------------------------------------------

    def select(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SELECT_LIMIT,
        include_unreviewed: bool = True,
    ) -> list[Skill]:
        """Return the skills RELEVANT to ``query``, most-relevant first, capped.

        Relevance uses :meth:`Skill.matches` (keyword / trigger match). Results
        are ranked by a light score (name hit > trigger hit > description hit)
        and truncated to ``limit`` for context discipline. This selection does
        NOT apply the review gate — that is enforced at injection time
        (:func:`runtime.skills.inject.compose_prompt`) so callers can still see
        which relevant skills were skipped for being unreviewed. Pass
        ``include_unreviewed=False`` to drop unreviewed skills here too.
        """
        if limit <= 0:
            return []
        scored: list[tuple[int, str, Skill]] = []
        for skill in self.all():
            if not include_unreviewed and not skill.reviewed:
                continue
            if not skill.matches(query):
                continue
            scored.append((self._score(skill, query), skill.name, skill))
        # Highest score first; stable tie-break on name for determinism.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [s for _, _, s in scored[:limit]]

    @staticmethod
    def _score(skill: Skill, query: str) -> int:
        q = query.lower()
        tokens = [t for t in q.replace("/", " ").replace("-", " ").split() if t]
        score = 0
        name = skill.name.lower()
        for tok in tokens:
            if tok in name:
                score += 5
        for trig in skill.triggers:
            t = trig.strip().lower()
            if not t:
                continue
            if t in q or any(tok in t for tok in tokens):
                score += 3
        for tok in tokens:
            if tok in skill.description.lower() or tok in skill.when_to_use.lower():
                score += 1
        return score
