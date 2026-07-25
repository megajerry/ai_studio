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
from ..event_types import EVENT_RETRO_COMPLETED, EVENT_VERIFY_FAILED, EVENT_WORK_RETRY
from ..memory import add_lesson as _add_lesson
from ..model.call import call_model
from ..model.registry import Registry
from ..models import Task, make_event
from ..trajectory_worker import rotate_mined_trajectories
from .critic import Critique
from .curator import CURATOR_TASK_TYPES

#: Role event (``retro.completed``): a retro distilled + stored N lessons for a
#: target task. Imported from the canonical :mod:`runtime.event_types`.

#: The queue task type the worker enqueues to run a retro (dispatched to run_retro).
RETRO_TASK_TYPE = "retro"

#: Hard cap on lessons per retro — bounds reflection (ADR-0003), never a loop.
MAX_LESSONS = 3

#: The internal handoff type the Retro enqueues when it nominates a clean, first-pass
#: WORK episode as a reusable procedure worth crystallizing (ADR-0024 P3 dual-source):
#: it enqueues a ``curate`` task (queue-only, never a direct call — invariant 1) and
#: the Curator then gates recurrence/maturity/efficiency over the whole cluster. This
#: is the ONLY type a retro ever enqueues, so it cannot recurse into a retro-of-a-retro.
CRYSTALLIZE_TASK_TYPE = CURATOR_TASK_TYPES[0]

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
    fail_count = event_types.count(EVENT_VERIFY_FAILED)
    retry_count = event_types.count(EVENT_WORK_RETRY)
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


def _worth_crystallizing(
    outcome: str, event_types: list[str], target_task_type: str
) -> bool:
    """Is this episode a candidate reusable WORK procedure worth crystallizing?

    Pure + deterministic. A clean, FIRST-PASS work success (no failed verify, no
    retry) is nominated to the Curator; a failed/retried episode is not a matured
    procedure, and meta-episodes (retro / curate / research / …) are never
    crystallized. The Retro only NOMINATES — the Curator gates recurrence + maturity
    + efficiency over the whole cluster before proposing a candidate.
    """
    if outcome == "failed":
        return False
    if EVENT_VERIFY_FAILED in event_types or EVENT_WORK_RETRY in event_types:
        return False
    return str(target_task_type or "").startswith("work")


class RetroResult(BaseModel):
    """What one retro produced (returned to the worker for the task result)."""

    target_task_id: Optional[str] = None
    target_task_type: str
    outcome: str
    #: Number of lessons distilled + stored (NEVER the lesson text).
    lessons_count: int
    #: Knowledge-layer ids of the stored lessons (ids only — no text).
    lesson_ids: list[str] = []
    #: The Critic's recommendation on the lessons, if a critic was consulted
    #: (``proceed`` / ``revise``); ``None`` when no critic was wired (ADR-0019).
    critic_recommendation: Optional[str] = None
    #: True when the retro nominated this episode to the Curator (enqueued a
    #: ``curate`` task, queue-only). ``curate_task_id`` is that task's id (id only).
    crystallize_enqueued: bool = False
    curate_task_id: Optional[str] = None


def _target_trajectory_id(conn: Any, target_id: Any) -> Optional[str]:
    """The reasoning-trajectory id linked to the mined task, or ``None``.

    A PM ``decompose`` step stamps ``tasks.trajectory_id`` (ADR-0020); this reads
    it back so the retro can mark the episode "mined". Best-effort and never fatal:
    a missing task / missing link / any read error yields ``None`` (the retro then
    behaves exactly as before). Returns the id as a string (body-free — an id only).
    """
    if not target_id:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trajectory_id FROM tasks WHERE id = %s", (target_id,))
            row = cur.fetchone()
        if not getattr(conn, "autocommit", True):
            conn.commit()
    except Exception:  # pragma: no cover - degrade to "no linked trajectory"
        return None
    if row and row.get("trajectory_id"):
        return str(row["trajectory_id"])
    return None


def rotate_ripe_trajectories(
    conn: Any,
    *,
    older_than_s: float,
    now: Optional[Any] = None,
    distill_fn: Optional[Callable[[Optional[str]], Optional[str]]] = None,
    limit: Optional[int] = None,
) -> list:
    """Learning-agent hook: distill OLD, already-MINED verbatim trajectories to lean.

    The Retro (the learning agent) DECIDES when to invoke this; it delegates to the
    guarded rotation in :mod:`runtime.trajectory_worker`, which only touches
    ``closed`` + ``verbatim`` episodes older than ``older_than_s`` that a retro has
    already mined (a ``retro.completed`` referencing them). Outcome-relevant fields
    (choice/confidence/refs/outcome) are preserved; only the verbatim ``rationale``
    body is distilled. Returns the ids actually rotated.
    """
    return rotate_mined_trajectories(
        conn, older_than_s=older_than_s, now=now, distill_fn=distill_fn, limit=limit
    )


def run_retro(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    model_registry: Optional[Registry] = None,
    max_lessons: int = MAX_LESSONS,
    critic: Optional[Callable[..., Critique]] = None,
    enqueue: Optional[Callable[..., Any]] = None,
    add_lesson: Callable[..., Any] = _add_lesson,
    read: Callable[..., list] = read_events,
) -> RetroResult:
    """Distill + store lessons for the target task referenced by ``task.payload``.

    ``task`` is a ``retro`` queue task carrying ``target_task_id`` /
    ``target_task_type`` / ``outcome``. Reads the target's event trail (falling back
    to the workstream trail), runs a traceability-only dry-run model call, distills
    up to ``max_lessons`` lessons, stores each in the Knowledge layer, and emits
    ``retro.completed`` (count + task ref only — never lesson text). ``add_lesson`` /
    ``read`` are injectable for tests.

    ``critic`` is the opt-in Critic consult (ADR-0019): when supplied (a
    :func:`runtime.roles.critic.run_critic`-shaped callable) it challenges the
    distilled lessons BEFORE they are stored — a single bounded, advisory consult
    that records the recommendation and emits ``critic.reviewed`` (counts only).
    With ``critic=None`` (default) the retro behaves exactly as before.

    ``enqueue`` is the opt-in dual-source handoff (ADR-0024 P3): when supplied (an
    :func:`runtime.tasks.enqueue_task`-shaped callable) and this episode is a clean,
    first-pass WORK success (:func:`_worth_crystallizing`), the retro enqueues ONE
    ``curate`` task (queue-only, invariant 1) nominating the episode to the Curator —
    which then gates recurrence/maturity/efficiency before proposing a candidate. It
    NEVER enqueues anything else (only ``curate``), so it cannot recurse into a
    retro-of-a-retro. With ``enqueue=None`` (default) the retro enqueues nothing —
    behavior-preserving.
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

    # 3. Distill (bounded, single-pass).
    lessons = distill_lessons(
        event_types, outcome=outcome, target_task_type=target_type, max_lessons=max_lessons
    )

    # 3b. Critic consult (ADR-0019, opt-in): the Critic challenges the distilled
    #     lessons — are these the right lessons? what is missing? — BEFORE they are
    #     stored. Bounded (a single consult, no loop) and advisory (a retro is not a
    #     human-gated commitment); it records the recommendation and emits
    #     `critic.reviewed` (counts only). With no critic wired this is skipped
    #     (behavior-preserving). Facts are counts/flags only — never lesson text.
    critic_recommendation: Optional[str] = None
    if critic is not None:
        has_prevention = any(
            "prevent" in text.lower() or "first attempt" in text.lower()
            for text in lessons
        )
        critique = critic(
            "lessons",
            {
                "kind": "lessons",
                "n_lessons": len(lessons),
                "outcome": outcome,
                "target_task_type": target_type,
                "has_prevention_lesson": has_prevention,
            },
            sink=sink,
            conn=conn,
            task_id=task.id,
            workstream=task.workstream,
            subject_kind="lessons",
        )
        critic_recommendation = critique.recommendation

    # 3c. Store each lesson in the Knowledge memory layer.
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

    # 3d. Dual-source handoff (ADR-0024 P3, opt-in): nominate a clean first-pass WORK
    #     episode to the Curator by enqueuing ONE `curate` task (queue-only, invariant
    #     1 — never a direct agent call). The Curator gates recurrence/maturity/
    #     efficiency over the whole cluster before proposing a candidate. Only ever a
    #     `curate` type is enqueued, so there is no retro-of-a-retro loop. Skipped when
    #     no enqueue seam is wired (behavior-preserving).
    curate_task_id: Optional[str] = None
    if enqueue is not None and _worth_crystallizing(outcome, event_types, target_type):
        curate_task = enqueue(
            conn,
            workstream=task.workstream,
            type=CRYSTALLIZE_TASK_TYPE,
            payload={"trigger": "retro", "target_task_type": target_type},
            priority=task.priority,
        )
        curate_task_id = str(getattr(curate_task, "id", "")) or None

    # 4. Emit retro.completed — COUNT + task ref only, NEVER the lesson text.
    #    When the mined episode has an associated reasoning trajectory (the target
    #    task carries a trajectory_id, ADR-0020), include its id: this body-free
    #    id is the "mined" marker the rotation worker keys on to safely rotate the
    #    now-mined verbatim trajectory to lean later (runtime.trajectory_worker).
    payload = {
        "target_task_id": str(target_id) if target_id else None,
        "target_task_type": target_type,
        "outcome": outcome,
        "lessons_count": len(lessons),
    }
    mined_traj_id = _target_trajectory_id(conn, target_id)
    if mined_traj_id is not None:
        payload["trajectory_id"] = mined_traj_id
    # Only present when the retro actually nominated the episode (body-free id).
    if curate_task_id is not None:
        payload["curate_task_id"] = curate_task_id
    sink.emit(
        make_event(
            workstream=task.workstream,
            type=EVENT_RETRO_COMPLETED,
            task_id=task.id,
            payload=payload,
        )
    )

    return RetroResult(
        target_task_id=str(target_id) if target_id else None,
        target_task_type=target_type,
        outcome=outcome,
        lessons_count=len(lessons),
        lesson_ids=lesson_ids,
        critic_recommendation=critic_recommendation,
        crystallize_enqueued=curate_task_id is not None,
        curate_task_id=curate_task_id,
    )
