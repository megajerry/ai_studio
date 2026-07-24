"""Inject selected skills into a role's prompt (the review gate lives here).

:func:`compose_prompt` appends the chosen skills' instructions to a base prompt
in a bounded, clearly-delimited ``### Skills`` section. Two invariants (ADR-0008):

1. **Review gate.** Only ``reviewed`` skills are injected by default. An
   unreviewed/untrusted skill is skipped and logged; including one requires an
   explicit ``allow_unreviewed=True`` (treat skills like code — review first).
2. **Instructions only, no execution.** A skill contributes TEXT to the prompt.
   Loading or injecting a skill NEVER runs its resources/scripts. Any execution
   still goes through the policy-gated tool layer (:func:`runtime.enforce.invoke`)
   — nothing here can trigger a side effect.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional
from uuid import UUID

from ..event_types import EVENT_SKILL_APPLIED
from ..models import make_event
from .models import Skill

logger = logging.getLogger("runtime.skills")

_SECTION_HEADER = "### Skills (on-demand capabilities — instructions only)"
_SECTION_NOTE = (
    "The following reviewed skills are relevant to this task. They provide "
    "INSTRUCTIONS only; any action still goes through a policy-gated tool "
    "(`invoke`). Do not execute a skill's scripts implicitly."
)
_SKILL_OPEN = "<skill name={name!r} source={source!r}>"
_SKILL_CLOSE = "</skill>"


class Composition(NamedTuple):
    """Result of composing a prompt: the text + what was in/out and why."""

    prompt: str
    included: list[Skill]
    skipped_unreviewed: list[Skill]


def filter_injectable(
    skills: list[Skill], *, allow_unreviewed: bool = False
) -> tuple[list[Skill], list[Skill]]:
    """Split ``skills`` into ``(injectable, skipped_unreviewed)`` per the gate."""
    included: list[Skill] = []
    skipped: list[Skill] = []
    for s in skills:
        if s.reviewed or allow_unreviewed:
            included.append(s)
        else:
            skipped.append(s)
    return included, skipped


def compose(
    base_prompt: str,
    skills: list[Skill],
    *,
    allow_unreviewed: bool = False,
) -> Composition:
    """Compose ``base_prompt`` + the reviewed subset of ``skills``.

    Returns a :class:`Composition` (prompt + which skills were included / skipped)
    so callers can log or emit telemetry on the selection.
    """
    included, skipped = filter_injectable(skills, allow_unreviewed=allow_unreviewed)
    for s in skipped:
        logger.info(
            "skipping unreviewed skill %r (source=%s) — not injected (review required)",
            s.name,
            s.source,
        )
    if allow_unreviewed:
        for s in skills:
            if not s.reviewed:
                logger.warning(
                    "injecting UNREVIEWED skill %r (source=%s) — allow_unreviewed=True",
                    s.name,
                    s.source,
                )

    if not included:
        return Composition(prompt=base_prompt, included=[], skipped_unreviewed=skipped)

    blocks: list[str] = [base_prompt.rstrip(), "", _SECTION_HEADER, _SECTION_NOTE, ""]
    for s in included:
        blocks.append(_SKILL_OPEN.format(name=s.name, source=s.source))
        if s.when_to_use:
            blocks.append(f"When to use: {s.when_to_use}")
        blocks.append(s.instructions.strip())
        blocks.append(_SKILL_CLOSE)
        blocks.append("")
    return Composition(
        prompt="\n".join(blocks).rstrip() + "\n",
        included=included,
        skipped_unreviewed=skipped,
    )


def compose_prompt(
    base_prompt: str,
    skills: list[Skill],
    *,
    allow_unreviewed: bool = False,
) -> str:
    """Convenience wrapper: return just the composed prompt string.

    Only relevant, REVIEWED skills are injected; unreviewed skills are skipped
    (and logged) unless ``allow_unreviewed=True``. See :func:`compose`.
    """
    return compose(base_prompt, skills, allow_unreviewed=allow_unreviewed).prompt


def emit_skill_applied(
    sink: Any,
    *,
    task_id: Optional[UUID],
    role: str,
    workstream: str,
    skills: Optional[list[Skill]],
    allow_unreviewed: bool = False,
) -> list[str]:
    """Emit ONE body-free ``skill.applied`` event for the skills actually injected.

    The P0 attribution hook (ADR-0024): a role calls this alongside its prompt
    composition, passing the SAME ``skills`` list it composed with. The injected
    subset is computed by the single injection gate (:func:`filter_injectable`, the
    same ``allow_unreviewed`` the prompt used), so this event names EXACTLY the
    skills that reached the prompt — reviewed ones by default, and an
    unreviewed/skipped skill is never attributed.

    BODY-FREE (invariants 5 & 6): the payload carries ONLY the injected skill
    ``skills`` NAME list + the acting ``role``; the ``task_id``/``workstream`` live
    on the envelope. It NEVER carries a skill's instruction body. When nothing was
    injected (no registry, no relevant skill, or every match unreviewed) it is a
    no-op — nothing was applied, so there is nothing to attribute. Returns the list
    of injected skill names (``[]`` when none) for the caller/tests.
    """
    if not skills:
        return []
    included, _skipped = filter_injectable(skills, allow_unreviewed=allow_unreviewed)
    names = [s.name for s in included]
    if not names:
        return []
    sink.emit(
        make_event(
            workstream=workstream,
            type=EVENT_SKILL_APPLIED,
            task_id=task_id,
            payload={"skills": names, "role": role},
        )
    )
    return names
