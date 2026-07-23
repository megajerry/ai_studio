"""Researcher role — mine external best-practice into reusable knowledge (ADR-0003).

The Researcher is the studio's *learn-from-outside* half (ADR-0003): where the
Retro distills lessons from an episode's own trail, the Researcher takes a
research topic/question and mines **external** best-practice/tools into reusable
:mod:`Knowledge <runtime.memory>` lessons (and, optionally, a candidate skill).
It is adaptive-lite: a fast-moving domain (AI/LLM/security/…) yields an extra
"revisit this" lesson (ADR-0003 "more research in fast-moving domains").

It acts through exactly the sanctioned seams — never agent-direct (architecture
§9, CLAUDE.md invariants 1-3):

- **Search only via the gateway** — ``search(conn, role="researcher", query=…)``
  (:mod:`runtime.search.gateway`): policy-gated on ``net.fetch``, cached, and
  keyless dry-run by default. The Researcher NEVER fetches the network itself.
- **Model call only via ``call_model``** — ``call_model(role="researcher",
  task_type="research", …)`` (dry-run, keyless): a routed/costed/logged synthesis
  step. Its text is NOT parsed for the decision; the lessons are distilled
  **deterministically** from the results so the loop is reproducible keyless.
- **Any file write via the policy-gated tool layer** — the optional candidate
  ``SKILL.md`` draft goes through ``invoke(role="researcher",
  tool_name="filesystem", op="write", …)``. A freshly drafted skill is
  ``reviewed: false`` (+ ``source`` provenance), so the inject gate NEVER
  auto-injects it (review-before-use, ADR-0008).

Invariants it upholds:

- **No research-loop.** A ``research`` task enqueues nothing — the Researcher
  distills into memory (and maybe drafts a candidate skill) and stops. There is
  no research-of-a-research.
- **Events leak no bodies.** ``research.completed`` carries only counts, a topic
  *hash*, and ids — never result bodies, the raw topic/query, or any secret text
  (invariants 5 & 6). ``add_lesson`` / ``search`` emit their own ids/dims-only
  events.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..memory import add_lesson as _add_lesson
from ..model.call import call_model as _call_model
from ..model.registry import Registry
from ..event_types import EVENT_RESEARCH_COMPLETED
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..search import SearchResult, search as _search
from ..tools import ToolRegistry

log = logging.getLogger("runtime.roles.researcher")

#: Role event (``research.completed``): a research task gathered N results and
#: distilled M lessons. Imported from the canonical :mod:`runtime.event_types`.

#: The queue task type the worker dispatches to :func:`run_research`.
RESEARCH_TASK_TYPE = "research"

#: The role name the policy gate checks (must be granted ``net.fetch``).
ROLE = "researcher"

#: Hard cap on lessons distilled per research task — bounds the output (ADR-0013).
MAX_LESSONS = 3

#: Default number of search hits to gather (the gateway caps + caches this).
DEFAULT_RESULTS = 5

#: Fast-moving domains where ADR-0003 wants research revisited more often. A topic
#: touching one of these earns an extra "revisit" lesson (adaptive-lite).
_FAST_MOVING = (
    "ai", "llm", "agent", "model", "security", "vulnerabilit", "framework",
    "library", "api", "protocol", "standard", "best practice", "best-practice",
    "tool", "release", "version",
)

# Research persona. The results digest is titles/urls only (no snippets), so no
# fetched body text reaches the model prompt. Its completion is logged for
# traceability but does NOT decide the lessons (they are distilled below).
_RESEARCH_PROMPT = (
    "You are the studio Researcher. Mine external best-practice for the topic and "
    "name the most reusable takeaways for future work. Topic: {topic}. "
    "Sources ({count}): {digest}"
)


def _resolve_topic(task: Task) -> str:
    """Resolve the research topic/question from the task payload."""
    payload = task.payload or {}
    for key in ("topic", "question", "goal", "objective"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "studio best practices"


def _topic_hash(topic: str) -> str:
    """A stable, PII-free digest of the topic for events (never the raw topic)."""
    return hashlib.sha256(topic.strip().lower().encode("utf-8")).hexdigest()[:16]


def _sources_digest(results: list[SearchResult]) -> str:
    """A compact titles/urls-only digest for the synthesis prompt (no snippets)."""
    if not results:
        return "(no sources)"
    return "; ".join(f"{r.title} <{r.url}>" for r in results[:DEFAULT_RESULTS])


def _slug(topic: str) -> str:
    """A filesystem-safe slug derived from the topic (bounded length)."""
    s = re.sub(r"[^a-z0-9]+", "-", topic.strip().lower()).strip("-")
    return (s or "research")[:48]


def distill_findings(
    topic: str,
    results: list[SearchResult],
    *,
    max_lessons: int = MAX_LESSONS,
) -> list[str]:
    """Distill 1-``max_lessons`` reusable lessons from a topic + its search hits.

    Pure + deterministic (no DB, no model, no network) so the research loop is
    reproducible and unit-testable. Adaptive-lite (ADR-0003): a fast-moving domain
    earns an extra "revisit this" lesson. Never returns empty.
    """
    n = len(results)
    top = results[0].title if results else "the available sources"
    lessons: list[str] = [
        f"For '{topic}', external research surfaced {n} source(s); the leading "
        f"reference is {top!r}. Consult the gathered best-practice before starting "
        "a task in this area rather than re-deriving it from scratch."
    ]
    low = topic.lower()
    if any(term in low for term in _FAST_MOVING):
        lessons.append(
            f"'{topic}' is a fast-moving area — treat researched best-practice as "
            "perishable and re-run the research (via the search gateway) before "
            "relying on it in new work (ADR-0003: more research in fast-moving domains)."
        )
    if n >= max(1, DEFAULT_RESULTS):
        lessons.append(
            f"Multiple independent sources agreed on '{topic}'; prefer the "
            "cross-referenced guidance and cite the source when applying it."
        )
    return lessons[: max(1, max_lessons)]


def _draft_candidate_skill(
    conn: Any,
    task: Task,
    topic: str,
    lessons: list[str],
    result_count: int,
    *,
    tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig],
    sink: EventSink,
    invoke_fn: Callable[..., Any],
) -> tuple[str, Optional[str]]:
    """Draft a ``reviewed: false`` candidate ``SKILL.md`` via the policy-gated tool.

    Returns ``(status, path)`` where ``status`` is the invoke outcome
    (``"executed"`` | ``"denied"`` | ``"pending"``). The frontmatter is
    ``reviewed: false`` + a ``source`` provenance line, so the inject gate
    (:func:`runtime.skills.inject.filter_injectable`) NEVER auto-injects it —
    review-before-use (ADR-0008). Writing needs ``fs.write``; a researcher without
    it is DENIED here (nothing written), which is a safe, logged no-op.
    """
    slug = _slug(topic)
    thash = _topic_hash(topic)
    body = "\n".join(f"- {t}" for t in lessons)
    content = (
        "---\n"
        f"name: {slug}\n"
        f"description: Candidate best-practice for '{topic}', drafted by the Researcher.\n"
        f"triggers: [{slug}]\n"
        f"when_to_use: When working on '{topic}'.\n"
        "reviewed: false\n"
        f"source: researcher; topic_hash={thash}; sources={result_count}\n"
        "---\n\n"
        f"# {topic}\n\n"
        "Drafted from external research. REVIEW before use (ADR-0008).\n\n"
        f"{body}\n"
    )
    path = f"candidates/{slug}/SKILL.md"
    result = invoke_fn(
        role=ROLE,
        tool_name="filesystem",
        registry=tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=path,
        content=content,
    )
    status = getattr(result.status, "value", str(result.status))
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    return status, (path if wrote else None)


class ResearchResult(BaseModel):
    """What one research task produced (returned to the worker for the task result).

    Carries counts/ids/hashes only — never result bodies, the raw topic, or any
    fetched text (invariants 5 & 6).
    """

    topic_hash: str
    results_count: int
    lessons_count: int
    #: Knowledge-layer ids of the stored lessons (ids only — no text).
    lesson_ids: list[str] = Field(default_factory=list)
    #: Candidate-skill draft outcome: "off" | "executed" | "denied" | "pending".
    skill_draft_status: str = "off"
    #: Path (tool-root-relative) of the drafted candidate skill, if written.
    skill_path: Optional[str] = None
    #: A drafted candidate skill is ALWAYS unreviewed (review-before-use, ADR-0008).
    skill_reviewed: bool = False


def run_research(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    model_registry: Optional[Registry] = None,
    tool_registry: Optional[ToolRegistry] = None,
    policy: Optional[PolicyConfig] = None,
    k: int = DEFAULT_RESULTS,
    max_lessons: int = MAX_LESSONS,
    draft_skill: bool = False,
    search: Callable[..., list[SearchResult]] = _search,
    call_model: Callable[..., Any] = _call_model,
    add_lesson: Callable[..., Any] = _add_lesson,
    invoke_fn: Callable[..., Any] = invoke,
) -> ResearchResult:
    """Service one ``research`` task: search → synthesize → distill into lessons.

    Resolves a topic from ``task.payload`` (``topic`` / ``question`` / ``goal``),
    gathers results through the **policy-gated cached search gateway**
    (``net.fetch``, keyless dry-run), runs a traceability-only dry-run synthesis
    ``call_model``, distills up to ``max_lessons`` reusable lessons into the
    workstream's Knowledge memory (``recall_lessons``-able), and — when
    ``draft_skill`` is set — drafts a ``reviewed: false`` candidate ``SKILL.md``
    via the policy-gated filesystem tool. Emits ``research.completed`` (counts /
    topic-hash / ids only). Enqueues NOTHING (no research-loop). ``search`` /
    ``call_model`` / ``add_lesson`` / ``invoke_fn`` are injectable for tests;
    ``policy`` gates both the search and the optional draft.

    Raises :class:`~runtime.search.SearchDenied` if the role lacks ``net.fetch`` —
    a genuine misconfiguration surfaced to the caller (nothing fetched/cached).
    """
    sink = sink or NullEventSink()
    topic = _resolve_topic(task)
    thash = _topic_hash(topic)

    # 1. Gather via the gateway ONLY (policy → cache → provider → cache; keyless).
    results = search(
        conn,
        ROLE,
        topic,
        k=k,
        sink=sink,
        policy=policy,
        workstream=task.workstream,
        task_id=task.id,
    )

    # 2. Traceability-only synthesis (dry-run, keyless). Digest is titles/urls only
    #    (no fetched body text); the completion does NOT decide the lessons.
    call_model(
        role=ROLE,
        task_type="research",
        messages=[
            {
                "role": "user",
                "content": _RESEARCH_PROMPT.format(
                    topic=topic, count=len(results), digest=_sources_digest(results)
                ),
            }
        ],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # 3. Distill (bounded, single-pass) into recallable Knowledge lessons.
    lessons = distill_findings(topic, results, max_lessons=max_lessons)
    lesson_ids: list[str] = []
    for text in lessons:
        item = add_lesson(
            conn,
            task.workstream,
            text,
            metadata={
                "source": "researcher",
                "topic_hash": thash,
                "results_count": len(results),
            },
        )
        if getattr(item, "id", None):
            lesson_ids.append(str(item.id))

    # 4. Optional candidate-skill draft (off by default) — reviewed: false, via the
    #    policy-gated tool. Denied cleanly when the role lacks fs.write (safe no-op).
    skill_status = "off"
    skill_path: Optional[str] = None
    if draft_skill and tool_registry is not None:
        skill_status, skill_path = _draft_candidate_skill(
            conn, task, topic, lessons, len(results),
            tool_registry=tool_registry, policy=policy, sink=sink, invoke_fn=invoke_fn,
        )

    # 5. Emit research.completed — counts / topic-hash / ids only, NEVER bodies.
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_RESEARCH_COMPLETED,
            task_id=task.id,
            payload={
                "topic_hash": thash,
                "results_count": len(results),
                "lessons_count": len(lessons),
                "skill_drafted": bool(skill_path),
            },
        )
    )

    return ResearchResult(
        topic_hash=thash,
        results_count=len(results),
        lessons_count=len(lessons),
        lesson_ids=lesson_ids,
        skill_draft_status=skill_status,
        skill_path=skill_path,
        skill_reviewed=False,
    )
