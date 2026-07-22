"""Retro role — distill durable lessons from an episode's event trail (ADR-0003).

The Retro role closes the learning loop's *learn + store* half: after a work
episode reaches a terminal state, it reads that task's replayable event trail,
distills 1-3 concise **lessons** (what worked / what to change), and stores each
in the **Knowledge** memory layer via :func:`runtime.memory.add_lesson`. Future
PM/Executor prompts then auto-inject the relevant ones
(:mod:`runtime.roles.lessons`) — so applying a lesson is deterministic, not a
matter of the model remembering (ADR-0003: prompt-level prevention > runtime
correction; cross-episode accumulation > single-pass reflection).

It obeys the studio invariants:

- **Bounded (no reflection loop).** At most :data:`MAX_LESSONS` lessons per retro,
  derived in a single pass from the trail (ADR-0003 caps reflection ~2). A retro
  NEVER enqueues another task, so there is no retro-of-a-retro loop.
- **Events carry no lesson text.** ``retro.completed`` records only the lesson
  COUNT + the target task reference. The lesson content lives in the Knowledge
  layer; ``add_lesson`` itself emits only ids/dims (invariants 5 & 6).
- **Model call is for traceability, not the decision.** A dry-run
  ``call_model(role="retro", task_type="retro", …)`` is logged like any model
  call, but the lessons are distilled **deterministically** from the trail so the
  loop is reproducible keyless.

Adaptive-lite intensity (ADR-0003 "more retro when error rate high"): a failed or
retried episode yields an extra prevention lesson; a clean pass yields one
"what worked" lesson. The worker's ``WORKER_RETRO`` policy decides *when* a retro
runs at all.
"""

from __future__ import annotations

from typing import Any, Callable, Optional
from uuid import UUID

from pydantic import BaseModel

from ..enforce import EventSink, NullEventSink
from ..events import read_events
from ..memory import add_lesson as _add_lesson
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event

#: Role event: a retro distilled + stored N lessons for a target task.
EVENT_RETRO_COMPLETED = "retro.completed"

#: The queue task type the worker enqueues to run a retro (dispatched to run_retro).
RETRO_TASK_TYPE = "retro"

#: Hard cap on lessons per retro — bounds reflection (ADR-0003), never a loop.
MAX_LESSONS = 3

# Retro persona. The trail summary is PII-free (event-type counts only); the model
# call is logged for traceability but does NOT decide the lessons.
_RETRO_PROMPT = (
    "You are the studio Retro. Review the episode's event trail summary and name "
    "the 1-3 most durable lessons (what worked / what to change) for future work. "
    "Episode outcome: {outcome}. Trail summary: {summary}"
)


def _trail_summary(event_types: list[str]) -> str:
    """A compact, PII-free digest of the trail: ``type×count`` pairs in order seen."""
    counts: dict[str, int] = {}
    for t in event_types:
        counts[t] = counts.get(t, 0) + 1
    return ", ".join(f"{t}×{n}" for t, n in counts.items()) or "(no events)"


def distill_lessons(
    event_types: list[str],
    *,
    outcome: str,
    target_task_type: str,
    max_lessons: int = MAX_LESSONS,
) -> list[str]:
    """Derive 1-``max_lessons`` concise lessons from an episode's event-type trail.

    Pure + deterministic (no DB, no model) so the learning loop is reproducible and
    unit-testable. Adaptive-lite: failures/retries add a prompt-level prevention
    lesson (ADR-0003); a clean pass records what worked. Never returns empty.
    """
    fail_count = event_types.count("verify.failed")
    retry_count = event_types.count("work.retry")
    failed = outcome == "failed" or fail_count > 0

    lessons: list[str] = []
    if failed:
        lessons.append(
            f"When a {target_task_type} task fails verification "
            f"({fail_count} failed check(s) this episode), define the concrete success "
            "marker up front and have the executor write that exact marker into its "
            "artifact, so the criterion is satisfied on the first attempt."
        )
    if retry_count:
        lessons.append(
            "Bounded re-enqueue recovered a failing work task; keep retries bounded "
            "and carry the verifier's reason forward as a nudge to the next attempt."
        )
    if not lessons:
        lessons.append(
            f"A {target_task_type} task passed verification on the first attempt by "
            "writing a concrete, independently checkable success marker into its "
            "artifact — keep defining an explicit marker before doing the work."
        )
    return lessons[: max(1, max_lessons)]


class RetroResult(BaseModel):
    """What one retro produced (returned to the worker for the task result)."""

    target_task_id: Optional[str] = None
    target_task_type: str
    outcome: str
    #: Number of lessons distilled + stored (NEVER the lesson text).
    lessons_count: int
    #: Knowledge-layer ids of the stored lessons (ids only — no text).
    lesson_ids: list[str] = []


def run_retro(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    model_registry: Optional[Registry] = None,
    max_lessons: int = MAX_LESSONS,
    add_lesson: Callable[..., Any] = _add_lesson,
    read: Callable[..., list] = read_events,
) -> RetroResult:
    """Distill + store lessons for the target task referenced by ``task.payload``.

    ``task`` is a ``retro`` queue task carrying ``target_task_id`` /
    ``target_task_type`` / ``outcome``. Reads the target's event trail (falling back
    to the workstream trail), runs a traceability-only dry-run model call, distills
    up to ``max_lessons`` lessons, stores each in the Knowledge layer, and emits
    ``retro.completed`` (count + task ref only — never lesson text). Never enqueues
    another task (no retro-loop). ``add_lesson``/``read`` are injectable for tests.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    target_id = payload.get("target_task_id")
    target_type = payload.get("target_task_type") or "work"
    outcome = payload.get("outcome") or "done"

    # 1. Read the target episode's replayable trail (deterministic seq order).
    events: list = []
    if target_id:
        try:
            events = read(conn, task_id=UUID(str(target_id)))
        except Exception:  # pragma: no cover - malformed id → fall back to workstream
            events = []
    if not events:
        events = read(conn, workstream=task.workstream)
    event_types = [getattr(e, "type", "") for e in events]

    # 2. Traceability-only model call (dry-run, keyless). Its text is NOT parsed;
    #    the lessons are distilled deterministically from the trail below.
    call_model(
        role="retro",
        task_type="retro",
        messages=[
            {
                "role": "user",
                "content": _RETRO_PROMPT.format(
                    outcome=outcome, summary=_trail_summary(event_types)
                ),
            }
        ],
        registry=model_registry,
        conn=conn,
        task_id=task.id,
        sink=sink,
        workstream=task.workstream,
    )

    # 3. Distill (bounded, single-pass) and store each lesson in Knowledge memory.
    lessons = distill_lessons(
        event_types, outcome=outcome, target_task_type=target_type, max_lessons=max_lessons
    )
    lesson_ids: list[str] = []
    for text in lessons:
        item = add_lesson(
            conn,
            task.workstream,
            text,
            metadata={
                "source": "retro",
                "target_task_type": target_type,
                "outcome": outcome,
            },
        )
        if getattr(item, "id", None):
            lesson_ids.append(str(item.id))

    # 4. Emit retro.completed — COUNT + task ref only, NEVER the lesson text.
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_RETRO_COMPLETED,
            task_id=task.id,
            payload={
                "target_task_id": str(target_id) if target_id else None,
                "target_task_type": target_type,
                "outcome": outcome,
                "lessons_count": len(lessons),
            },
        )
    )

    return RetroResult(
        target_task_id=str(target_id) if target_id else None,
        target_task_type=target_type,
        outcome=outcome,
        lessons_count=len(lessons),
        lesson_ids=lesson_ids,
    )
