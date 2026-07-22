"""Lesson injection — auto-apply durable Retro lessons at prompt assembly (M4+).

This is the deterministic "apply the lesson" half of the learning loop (ADR-0003):
the Retro role distills lessons into the Knowledge memory layer, and *this* module
injects the relevant ones into a role's prompt **before it acts** — so applying a
lesson never depends on the model happening to remember it.

It mirrors :func:`runtime.skills.inject.compose_prompt` (ADR-0008): a bounded,
clearly-delimited ``### Lessons`` section appended to the base prompt. Two
invariants:

1. **Bounded + scoped (ADR-0013).** Only the top-``k`` lessons relevant to the
   query are recalled, and at most ``limit`` are injected. Recall is
   workstream-scoped (:func:`runtime.memory.recall_lessons`) — a workstream never
   sees another's private lessons (global lessons are shared deliberately).
2. **Behavior-preserving.** With no ``conn``, no workstream, no recalled lessons,
   or a recall error, the base prompt is returned **unchanged** — a role with an
   empty lessons corpus behaves exactly as before.

Like a skill, a lesson is TEXT injected into a prompt: injecting one NEVER runs
anything. Any action still goes through the policy-gated tool path (`invoke`).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..memory import recall_lessons as _recall_lessons

logger = logging.getLogger("runtime.roles.lessons")

#: Default cap on how many lessons are injected into one prompt (ADR-0013 bound).
DEFAULT_LIMIT = 3

#: Recommended production cosine floor for lesson recall. A modest floor keeps
#: only genuinely relevant lessons out of a prompt (a weak, off-topic match is
#: worse than no lesson). ~0.2 suits the dry-run embedder; tune per real
#: embedding model. Left as guidance — the default below is ``None`` (no floor,
#: behavior-preserving); a caller opts in by passing ``min_score``.
RECOMMENDED_MIN_SCORE = 0.2

_SECTION_HEADER = "### Lessons (durable know-how from prior retros — apply these)"
_SECTION_NOTE = (
    "Retros distilled these lessons for this workstream. Apply them proactively "
    "to avoid repeating past mistakes. They are guidance (instructions only) — any "
    "action still goes through a policy-gated tool (`invoke`)."
)


def compose_lessons(
    base_prompt: str,
    lessons: list[str],
    *,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Append up to ``limit`` ``lessons`` to ``base_prompt`` in a bounded section.

    Empty/blank lessons are dropped. With no usable lessons the base prompt is
    returned unchanged (behavior-preserving). Pure — no DB, no events.
    """
    usable = [text.strip() for text in lessons if text and text.strip()][: max(0, limit)]
    if not usable:
        return base_prompt

    blocks: list[str] = [base_prompt.rstrip(), "", _SECTION_HEADER, _SECTION_NOTE, ""]
    for text in usable:
        blocks.append(f"- {text}")
    return "\n".join(blocks).rstrip() + "\n"


def recall_lesson_texts(
    conn: Any,
    workstream: Optional[str],
    query: str,
    *,
    k: int = DEFAULT_LIMIT,
    min_score: Optional[float] = None,
    recall: Callable[..., list] = _recall_lessons,
) -> list[str]:
    """Recall the lesson TEXTS most relevant to ``query`` (workstream-scoped).

    The recall half of :func:`inject_lessons`, exposed so the shared prompt
    assembler (:mod:`runtime.roles.prompt`) can layer lessons alongside charter /
    overlay / skills without re-implementing recall. Returns ``[]`` when there is
    no ``conn``/``workstream``, when nothing is recalled, or when recall fails —
    so a role is never blocked by the learning layer. See :func:`inject_lessons`
    for the ``min_score`` floor semantics.
    """
    if conn is None or not workstream:
        return []
    # Only thread min_score when set, so a custom `recall` seam whose signature
    # predates the floor keeps working (behavior-preserving when min_score=None).
    extra = {} if min_score is None else {"min_score": min_score}
    try:
        items = recall(conn, workstream, query, k=k, **extra)
    except Exception:  # pragma: no cover - defensive: never let recall break a role
        logger.exception("lesson recall failed; proceeding with base prompt")
        return []
    return [getattr(it, "text", "") for it in items]


def inject_lessons(
    base_prompt: str,
    conn: Any,
    workstream: Optional[str],
    query: str,
    *,
    k: int = DEFAULT_LIMIT,
    limit: int = DEFAULT_LIMIT,
    min_score: Optional[float] = None,
    recall: Callable[..., list] = _recall_lessons,
) -> str:
    """Recall the lessons most relevant to ``query`` and inject them into the prompt.

    The deterministic apply-the-lesson step a role runs before acting. Recall is
    workstream-scoped (plus the shared global corpus). Returns ``base_prompt``
    unchanged when there is no ``conn``/``workstream``, when nothing is recalled,
    or when recall fails — so a role is never blocked by the learning layer.

    ``min_score`` is an optional cosine floor threaded into recall to drop weakly
    relevant lessons; ``None`` (default) applies no floor (behavior-preserving).
    See :data:`RECOMMENDED_MIN_SCORE` for the suggested production value.
    """
    texts = recall_lesson_texts(
        conn, workstream, query, k=k, min_score=min_score, recall=recall
    )
    return compose_lessons(base_prompt, texts, limit=limit)
