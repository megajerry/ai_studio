"""Skill model + parse errors (Agent Skills open standard, ADR-0008).

A *skill* is a portable, reviewable capability package: a ``SKILL.md`` file =
YAML frontmatter (metadata) + a markdown body (the instructions), plus optional
sibling resources (scripts / templates) referenced by relative path. Skills are
loaded **on demand** and injected into a role's prompt only when relevant — they
carry INSTRUCTIONS only and never execute anything themselves (ADR-0008 / §14).

This module is pure data + validation: parsing lives in :mod:`.loader`,
discovery/selection in :mod:`.registry`, and prompt composition in :mod:`.inject`.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class SkillError(Exception):
    """A skill could not be parsed or is invalid.

    Raised with a clear, path-qualified message so a malformed skill fails
    *gracefully* (the registry logs + skips it) rather than crashing discovery.
    """


class Skill(BaseModel):
    """One parsed ``SKILL.md`` — frontmatter metadata + the instruction body.

    Required (per the open standard): ``name`` + ``description``. The rest are
    studio extensions that make on-demand selection + the review gate work:

    - ``triggers`` — keywords / task-types / roles that make this skill relevant
      (``when_to_use`` is the human-readable version of the same intent);
    - ``reviewed`` — the supply-chain gate. Skills are treated like code: an
      unreviewed skill is **excluded from injection by default** (ADR-0008);
    - ``source`` — provenance (``in-repo``, a library name, a URL);
    - ``resources`` — relative paths to sibling scripts/templates. These are
      **never auto-executed** by loading a skill; any execution still goes
      through the policy-gated tool layer (:func:`runtime.enforce.invoke`).
    """

    name: str
    description: str
    instructions: str = Field(
        default="",
        description="The markdown body — the actual guidance injected into a prompt.",
    )
    triggers: list[str] = Field(default_factory=list)
    when_to_use: str = ""
    resources: list[str] = Field(default_factory=list)
    #: Provenance for the review gate (ADR-0008: prefer audited sources).
    source: str = "unknown"
    #: Review gate: only reviewed skills are injected by default.
    reviewed: bool = False
    #: Absolute path of the SKILL.md this was parsed from (set by the loader).
    path: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("skill 'name' must be a non-empty string")
        return v

    @field_validator("description")
    @classmethod
    def _desc_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("skill 'description' must be a non-empty string")
        return v

    def matches(self, query: str) -> bool:
        """True if ``query`` (a role, task_type, or free text) is relevant here.

        Relevance is a case-insensitive keyword match against the skill's
        ``name``, ``triggers``, ``when_to_use`` and ``description`` — the
        on-demand selection heuristic (ADR-0013: load only what's relevant).
        """
        q = (query or "").strip().lower()
        if not q:
            return False
        haystacks = [self.name.lower(), self.when_to_use.lower(), self.description.lower()]
        haystacks.extend(t.lower() for t in self.triggers)
        hay = " \n ".join(haystacks)
        # Match if any query token appears in the haystack, OR a declared trigger
        # appears inside the query (so "plan the release" hits trigger "plan").
        tokens = [t for t in q.replace("/", " ").replace("-", " ").split() if t]
        for tok in tokens:
            if tok in hay:
                return True
        for trig in self.triggers:
            t = trig.strip().lower()
            if t and t in q:
                return True
        return False
