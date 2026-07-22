"""Shared role prompt-assembly layer — the vertical-customization seam (ADR-0003).

A role is ``prompt + skills + tools`` (architecture §3). Historically each role
built its own prompt inline: base persona → skills (:func:`runtime.skills.compose_prompt`)
→ lessons (:func:`runtime.roles.lessons.compose_lessons`). This module lifts that
into a **single assembler** so every role composes its prompt the same way and a
**vertical can inject its own framing** (a workstream charter + a per-role overlay)
*without editing role code* — the config-driven half of the workstream-bootstrap
primitive (state/backlog.md).

:func:`compose_role_prompt` layers, in this fixed order, each in a bounded,
clearly-delimited section (context discipline, ADR-0013):

1. **shared role base** — the platform's persona for the role (unchanged);
2. **workstream charter** — the vertical's mission / operating context (config);
3. **per-role overlay** — the vertical's specialization of *this* role (config);
4. **skills** — the relevant, REVIEWED on-demand skills (ADR-0008), reusing
   :func:`runtime.skills.compose_prompt` verbatim;
5. **lessons** — the durable lessons prior retros distilled + this role recalled
   (ADR-0003), reusing :func:`runtime.roles.lessons.compose_lessons` verbatim;
6. **task specifics** — extra per-task context a vertical wants appended.

**Behavior-preserving default.** With no charter, overlay, or task, and the same
skills/lessons the role passed before, the output is *identical* to the previous
inline composition — existing role/verifier tests stay green. Charter/overlay are
passed in (the workstream-bootstrap primitive supplies them later); today they
default to ``None`` so nothing changes until a vertical opts in.

Like skills and lessons, every layer here is TEXT: assembling a prompt NEVER runs
anything. Any action still flows through the policy-gated tool path (`invoke`).
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..skills import Skill, compose_prompt
from .lessons import compose_lessons

#: Bounded, delimited section framing for the config-driven layers. Mirrors the
#: ``### Skills`` / ``### Lessons`` sections so the whole prompt reads uniformly.
_CHARTER_HEADER = "### Workstream charter (this vertical's mission + operating context)"
_CHARTER_NOTE = (
    "You operate inside this workstream. Keep its mission, constraints, and "
    "domain context in mind; it frames how you apply your role below."
)
_OVERLAY_HEADER = "### Role overlay (this vertical's specialization of your role)"
_OVERLAY_NOTE = (
    "This workstream specializes your role as follows. It refines — it does not "
    "override — the platform role responsibilities and safety rules above."
)
_TASK_HEADER = "### Task (the specific work in front of you)"
_TASK_NOTE = "Concrete context for the task you are acting on right now."


def _section(prompt: str, header: str, note: str, body: str) -> str:
    """Append one bounded, clearly-delimited section to ``prompt``.

    Empty/blank ``body`` is a no-op (the section is skipped) so an absent layer
    never leaves a dangling header — behavior-preserving.
    """
    body = (body or "").strip()
    if not body:
        return prompt
    blocks = [prompt.rstrip(), "", header, note, "", body, ""]
    return "\n".join(blocks).rstrip() + "\n"


def compose_role_prompt(
    role_base: str,
    *,
    workstream_charter: Optional[str] = None,
    role_overlay: Optional[str] = None,
    skills: Optional[Sequence[Skill]] = None,
    lessons: Optional[Sequence[str]] = None,
    task: Optional[str] = None,
    allow_unreviewed: bool = False,
) -> str:
    """Assemble a role's full prompt from its layers (see the module docstring).

    ``role_base`` is the shared platform persona for the role. ``workstream_charter``
    and ``role_overlay`` are the vertical's config-driven framing (``None`` →
    omitted, behavior-preserving). ``skills`` is the ALREADY-SELECTED skill list
    (the role runs its own role-specific ``registry.select(query)``); only the
    reviewed subset is injected (:func:`runtime.skills.compose_prompt`), unless
    ``allow_unreviewed``. ``lessons`` is the ALREADY-RECALLED lesson texts
    (:func:`runtime.roles.lessons.recall_lesson_texts`). ``task`` is optional extra
    per-task context.

    With every optional layer absent the exact ``role_base`` is returned unchanged.
    Each present layer is added in a bounded, delimited section reusing the same
    composers the roles used inline, so a role that only passes ``skills`` /
    ``lessons`` gets byte-identical output to before this assembler existed.
    """
    prompt = role_base
    prompt = _section(prompt, _CHARTER_HEADER, _CHARTER_NOTE, workstream_charter or "")
    prompt = _section(prompt, _OVERLAY_HEADER, _OVERLAY_NOTE, role_overlay or "")
    if skills:
        prompt = compose_prompt(prompt, list(skills), allow_unreviewed=allow_unreviewed)
    if lessons:
        prompt = compose_lessons(prompt, list(lessons))
    prompt = _section(prompt, _TASK_HEADER, _TASK_NOTE, task or "")
    return prompt
