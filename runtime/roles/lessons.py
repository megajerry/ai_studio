"""Lesson injection — auto-apply durable Retro lessons at prompt assembly (M4+).

This is the deterministic "apply the lesson" half of the learning loop (ADR-0003):
the Retro role distills lessons into the Knowledge memory layer, and *this* module
injects the relevant ones into a role's prompt **before it acts** — so applying a
lesson never depends on the model happening to remember it.

It mirrors :func:`runtime.skills.inject.compose_prompt` (ADR-0008): a bounded,
clearly-delimited ``### Lessons`` section appended to the base prompt. Two
invariants:

1. **Bounded + scoped + relevance-floored (ADR-0013).** Only the top-``k``
   lessons relevant to the query are recalled, and at most ``limit`` are injected.
   A cosine relevance floor (``min_score``, default :data:`RECOMMENDED_MIN_SCORE`)
   drops weakly/irrelevant candidates BEFORE top-N, so an injected lesson is
   always genuinely relevant (a barely-related lesson pollutes the prompt and
   hurts quality). Recall is workstream-scoped
   (:func:`runtime.memory.recall_lessons`) — a workstream never sees another's
   private lessons (global lessons are shared deliberately).
2. **Behavior-preserving.** With no ``conn``, no workstream, no recalled lessons,
   or a recall error, the base prompt is returned **unchanged** — a role with an
   empty lessons corpus behaves exactly as before.

Like a skill, a lesson is TEXT injected into a prompt: injecting one NEVER runs
anything. Any action still goes through the policy-gated tool path (`invoke`).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from ..memory import recall_lessons as _recall_lessons

logger = logging.getLogger("runtime.roles.lessons")

#: Default cap on how many lessons are injected into one prompt (ADR-0013 bound).
DEFAULT_LIMIT = 3

#: Default cosine floor applied to lesson recall BEFORE top-N selection, so an
#: injected lesson is always at least this relevant (a weak, off-topic match is
#: worse than no lesson — it pollutes the prompt and hurts quality).
#:
#: Justification (measured on the dry-run embedder, which is deterministic —
#: signed feature-hashing of character n-grams, see runtime/memory/embed.py):
#: genuinely on-topic lesson/query pairs score ~0.5-0.9 cosine, while clearly
#: irrelevant pairs score roughly -0.1..+0.1. ``0.2`` sits in the empty gap
#: between the two clusters, so it excludes clearly-irrelevant lessons while
#: keeping every genuinely-relevant one — conservative by design (no
#: empty-injection regression: the existing learning-loop recalls all clear it).
#:
#: PRE-REAL-EMBEDDINGS DEFAULT: this floor is tuned to the dry-run embedder and
#: MUST be re-tuned once a real embedding model lands (its cosine scale differs).
#: Tune without a code change via the ``AI_STUDIO_LESSON_MIN_SCORE`` env var.
RECOMMENDED_MIN_SCORE = 0.2

#: Env override for the floor (a float). Empty/unset → :data:`RECOMMENDED_MIN_SCORE`.
_MIN_SCORE_ENV = "AI_STUDIO_LESSON_MIN_SCORE"

#: Sentinel for "caller did not pass ``min_score``" → use the env/default floor.
#: Distinct from an explicit ``None``, which DISABLES the floor entirely (recall
#: returns its top-N unfiltered — the old behavior, kept as an escape hatch).
_DEFAULT_FLOOR: Any = object()


def _resolve_min_score(min_score: Any) -> Optional[float]:
    """Resolve the effective floor: explicit caller value wins; else env; else default.

    - an explicit ``float`` → used as-is;
    - an explicit ``None`` → no floor (escape hatch);
    - omitted (:data:`_DEFAULT_FLOOR`) → ``AI_STUDIO_LESSON_MIN_SCORE`` if set to a
      valid float, otherwise :data:`RECOMMENDED_MIN_SCORE`.
    """
    if min_score is not _DEFAULT_FLOOR:
        return min_score  # explicit float floor, or None to disable
    raw = os.environ.get(_MIN_SCORE_ENV, "").strip()
    if not raw:
        return RECOMMENDED_MIN_SCORE
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r; using default lesson floor %.2f",
            _MIN_SCORE_ENV,
            raw,
            RECOMMENDED_MIN_SCORE,
        )
        return RECOMMENDED_MIN_SCORE

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
    min_score: Any = _DEFAULT_FLOOR,
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
    floor = _resolve_min_score(min_score)
    # Only thread min_score when a floor applies, so an explicit None (escape
    # hatch) — and a custom `recall` seam whose signature predates the floor —
    # keep working unfiltered.
    extra = {} if floor is None else {"min_score": floor}
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
    min_score: Any = _DEFAULT_FLOOR,
    recall: Callable[..., list] = _recall_lessons,
) -> str:
    """Recall the lessons most relevant to ``query`` and inject them into the prompt.

    The deterministic apply-the-lesson step a role runs before acting. Recall is
    workstream-scoped (plus the shared global corpus). Returns ``base_prompt``
    unchanged when there is no ``conn``/``workstream``, when nothing is recalled,
    or when recall fails — so a role is never blocked by the learning layer.

    ``min_score`` is the cosine relevance floor applied BEFORE top-N selection so
    an injected lesson is always at least that relevant. Omitted → the
    :data:`RECOMMENDED_MIN_SCORE` default (overridable via ``AI_STUDIO_LESSON_MIN_SCORE``);
    pass an explicit ``float`` to override, or ``None`` to disable the floor.
    """
    texts = recall_lesson_texts(
        conn, workstream, query, k=k, min_score=min_score, recall=recall
    )
    return compose_lessons(base_prompt, texts, limit=limit)
