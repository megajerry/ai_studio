"""Decision store — the ASYNC open-ended stakeholder-decision loop (ADR-0025).

The open-ended analogue of the binary approval loop (:mod:`runtime.approvals`).
A 🔴 approval is approve/deny; a **decision** is a real question the stakeholder
must answer — a chosen option or free text ("which vendor?", "what tone?"). Because
the stakeholder is not monitoring 24/7 (ADR-0006: batched, async, human < 4hrs/day),
a decision must never BLOCK a worker. So, exactly mirroring the approval discipline:

    request_decision(dependent_task) → [open]  + PARK the task (in_progress→blocked)
        → the worker is FREED (grabs other up_for_grabs work; the blocked one is
          not grabbable), so nothing stalls waiting on the human
        → human answers (later, whenever): answer_decision → [answered]
        → RESUME the parked task (blocked→up_for_grabs) so a fresh worker re-grabs
          it and reads the chosen answer via get_decision.

Both moves reuse the SINGLE guarded lifecycle writer :func:`runtime.tasks.transition`
(the ``blocked ↔ up_for_grabs`` edges already used by approvals) — there is NO
ad-hoc status UPDATE here (invariant 4). The QUESTION and ANSWER text live in the
``decisions`` row ONLY; the event log carries just ids / workstream / seq / status /
has_options / resolver (body-free — invariants 5 & 6, mirroring approvals /
trajectory). Every function takes an open ``conn`` (the caller owns the transaction
boundary, matching :mod:`runtime.approvals` / :mod:`runtime.tasks`) and an
:class:`~runtime.enforce.EventSink` for observability.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .event_types import EVENT_DECISION_ANSWERED, EVENT_DECISION_REQUESTED
from .models import TaskStatus, make_event
from .tasks import transition

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .enforce import EventSink

#: Statuses a decision row may hold.
STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_WITHDRAWN = "withdrawn"

#: The ``tasks.result`` key that links a parked task back to its decision (mirrors
#: approvals' ``blocked_on_approval``; kept DISTINCT so the approval resume sweep
#: (``resume_approved``) never mistakes a decision-parked task for an approval one).
BLOCKED_ON_DECISION = "blocked_on_decision"
#: The reason recorded on the park transition (a short CODE, never body text).
PARK_REASON = "awaiting_decision"


class DecisionParkError(RuntimeError):
    """A decision's dependent task could NOT be parked, so no decision was created.

    Raised by :func:`request_decision` when the park (``in_progress → blocked``)
    is a guarded no-op — the task already left ``in_progress`` in the window (e.g.
    a supervisor re-kick did ``in_progress → up_for_grabs``). Park and record are
    ATOMIC (one transaction, park FIRST): on this failure the whole operation is
    rolled back, so NO orphan ``open`` decision is ever committed against a task
    that is still runnable. The caller must treat this as "the decision was not
    raised" and retry / handle it (never assume the task is parked).
    """

_COLUMNS = (
    "id, workstream, question, options, status, answer, answered_by, "
    "dependent_task_id, default_choice, seq, created_at, answered_at"
)


class Decision(BaseModel):
    """A persisted decision row (an open-ended question awaiting / holding an answer)."""

    id: UUID
    workstream: str
    question: str
    options: Optional[list[str]] = None
    status: str
    answer: Optional[str] = None
    answered_by: Optional[str] = None
    dependent_task_id: Optional[UUID] = None
    default_choice: Optional[str] = None
    seq: Optional[int] = None
    created_at: datetime
    answered_at: Optional[datetime] = None

    @property
    def has_options(self) -> bool:
        return bool(self.options)


class DecisionDigest(BaseModel):
    """A batched summary of open decisions for the Spokesman (ADR-0006)."""

    count: int
    items: list[Decision] = Field(default_factory=list)


def _emit(
    sink: "EventSink", *, type: str, decision: Decision, workstream: str
) -> None:
    """Emit a decision.* event carrying ids/status/seq/has_options — never the text.

    The question and answer BODIES stay in the ``decisions`` row (invariants 5 & 6);
    the wire carries only the decision's identity + shape, mirroring approvals.*.
    """
    sink.emit(
        make_event(
            workstream=workstream,
            type=type,
            task_id=decision.dependent_task_id,
            payload={
                "decision_id": str(decision.id),
                "workstream": decision.workstream,
                "seq": decision.seq,
                "status": decision.status,
                "has_options": decision.has_options,
                "has_default": decision.default_choice is not None,
                "dependent_task_id": (
                    str(decision.dependent_task_id)
                    if decision.dependent_task_id else None
                ),
                "answered_by": decision.answered_by,
            },
        )
    )


def request_decision(
    conn: psycopg.Connection,
    *,
    workstream: str,
    question: str,
    options: Optional[list[str]] = None,
    dependent_task_id: Optional[UUID] = None,
    default_choice: Optional[str] = None,
    sink: "EventSink",
    now: Optional[datetime] = None,
) -> Decision:
    """Raise an ``open`` decision; PARK its dependent task so the worker is freed.

    Inserts an ``open`` row (question/options/default_choice live in the row, never
    on the wire). When ``dependent_task_id`` is given, that task is PARKED via the
    single guarded :func:`runtime.tasks.transition` (``in_progress → blocked``, the
    same edge approvals use), stamping ``result = {blocked_on_decision, reason:
    awaiting_decision}`` so :func:`answer_decision` can find + resume it. Parking a
    ``blocked`` task removes it from the grab pool, so the worker immediately grabs
    OTHER ``up_for_grabs`` work — a decision never stalls the fleet.

    ``options=None`` means a free-text answer is allowed; a list means the answer
    should be one of the choices. Emits a body-free ``decision.requested`` (id /
    workstream / seq / has_options — NOT the question text). ``now`` is injectable
    for deterministic tests (defaults to the DB clock). Returns the created
    :class:`Decision` (its ``.id`` is the decision id).

    **Atomicity (park-then-record).** Park and record run in ONE transaction, park
    FIRST: when a ``dependent_task_id`` is given the task is PARKED via the single
    guarded :func:`runtime.tasks.transition` and its return is CHECKED; only if the
    park SUCCEEDED is the ``open`` decision row INSERTed. If the park is a guarded
    no-op (the task already left ``in_progress`` in the window — e.g. a supervisor
    re-kick did ``in_progress → up_for_grabs``) the whole operation ABORTS with
    :class:`DecisionParkError` and rolls back, so an ``open`` decision is NEVER
    committed against a still-runnable task (which would bypass the decision gate
    and silently discard the human's answer when the later resume no-ops).
    """
    opts = list(options) if options is not None else None
    # Pre-generate the id so the park can stamp `blocked_on_decision` BEFORE the row
    # exists — park-first means a failed park never even reaches the INSERT.
    decision_id = uuid4()
    with conn.transaction():
        # Park the dependent task FIRST so the worker moves on to other work, and
        # CHECK the return. Reuses the guarded in_progress→blocked edge (like
        # runtime.tasks.block_task); guarded to in_progress. A None return means the
        # task is no longer in_progress (raced by a re-kick / other move): ABORT the
        # whole request — raising rolls back this transaction so no `open` decision
        # is left paired with a runnable task.
        if dependent_task_id is not None:
            parked = transition(
                conn, dependent_task_id, TaskStatus.BLOCKED,
                expected_from=TaskStatus.IN_PROGRESS,
                result={
                    BLOCKED_ON_DECISION: str(decision_id),
                    "reason": PARK_REASON,
                },
            )
            if parked is None:
                raise DecisionParkError(
                    f"cannot park task {dependent_task_id} for a decision: it is not "
                    "in_progress (likely re-kicked or already moved); no decision created"
                )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO decisions
                    (id, workstream, question, options, dependent_task_id,
                     default_choice, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'open', COALESCE(%s, now()))
                RETURNING {_COLUMNS}
                """,
                (
                    decision_id,
                    workstream,
                    question,
                    Jsonb(opts) if opts is not None else None,
                    dependent_task_id,
                    default_choice,
                    now,
                ),
            )
            decision = Decision.model_validate(cur.fetchone())

        _emit(sink, type=EVENT_DECISION_REQUESTED, decision=decision, workstream=workstream)
    return decision


def answer_decision(
    conn: psycopg.Connection,
    decision_id: UUID,
    answer: str,
    resolver: str,
    sink: "EventSink",
    *,
    now: Optional[datetime] = None,
) -> Optional[Decision]:
    """Answer an ``open`` decision and RESUME its parked dependent task.

    Guarded to ``open`` (an already-answered / withdrawn decision is never
    re-answered: on such a conflict — or a missing id — nothing changes, no event
    is emitted, and ``None`` is returned). Records ``answered`` + answer +
    answered_by + answered_at (the answer text lives in the row, not on the wire).
    Emits a body-free ``decision.answered`` (id / status / resolver — NO answer
    text). If a ``dependent_task_id`` is still ``blocked``, it is RESUMED via the
    guarded ``blocked → up_for_grabs`` edge (claim cleared) so a fresh worker
    re-grabs it and reads the chosen answer via :func:`get_decision`. ``now`` is
    injectable for deterministic tests.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE decisions
                SET status = 'answered', answer = %s, answered_by = %s,
                    answered_at = COALESCE(%s, now())
                WHERE id = %s AND status = 'open'
                RETURNING {_COLUMNS}
                """,
                (answer, resolver, now, decision_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            decision = Decision.model_validate(row)

        # Resume the parked task (blocked → up_for_grabs), clearing the claim so a
        # fresh worker re-grabs it. Guarded to `blocked`, so a task that already
        # moved (e.g. abandoned) is left untouched.
        if decision.dependent_task_id is not None:
            transition(
                conn, decision.dependent_task_id, TaskStatus.UP_FOR_GRABS,
                expected_from=TaskStatus.BLOCKED, clear_claim=True,
            )

        _emit(sink, type=EVENT_DECISION_ANSWERED, decision=decision, workstream=decision.workstream)
    return decision


def withdraw_decision(
    conn: psycopg.Connection,
    decision_id: UUID,
    resolver: str,
    sink: "EventSink",
    *,
    now: Optional[datetime] = None,
) -> Optional[Decision]:
    """Withdraw an ``open`` decision (no longer needed); RESUME its parked task.

    Guarded to ``open`` (returns ``None`` otherwise). The dependent task is still
    resumed (``blocked → up_for_grabs``) so it never stays parked forever — the
    resumed task reads the ``withdrawn`` status and can fall back to
    ``default_choice``. Emits a body-free ``decision.answered`` carrying the
    ``withdrawn`` status (the resolution signal; no body text).
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE decisions
                SET status = 'withdrawn', answered_by = %s,
                    answered_at = COALESCE(%s, now())
                WHERE id = %s AND status = 'open'
                RETURNING {_COLUMNS}
                """,
                (resolver, now, decision_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            decision = Decision.model_validate(row)

        if decision.dependent_task_id is not None:
            transition(
                conn, decision.dependent_task_id, TaskStatus.UP_FOR_GRABS,
                expected_from=TaskStatus.BLOCKED, clear_claim=True,
            )

        _emit(sink, type=EVENT_DECISION_ANSWERED, decision=decision, workstream=decision.workstream)
    return decision


def get_decision(conn: psycopg.Connection, decision_id: UUID | str) -> Optional[Decision]:
    """Fetch one decision by id regardless of status, or ``None`` if absent.

    The read the RESUMED task uses to pull the chosen answer + status. Does not open
    a lingering transaction on a non-autocommit connection.
    """
    if isinstance(decision_id, str):
        decision_id = UUID(decision_id)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM decisions WHERE id = %s", (decision_id,))
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    if row is None:
        return None
    return Decision.model_validate(row)


def open_decisions(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> list[Decision]:
    """List currently-``open`` decisions (oldest first), optionally per workstream."""
    clauses = ["status = 'open'"]
    params: list[Any] = []
    if workstream is not None:
        clauses.append("workstream = %s")
        params.append(workstream)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM decisions WHERE {' AND '.join(clauses)} "
            "ORDER BY seq ASC",
            params,
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [Decision.model_validate(r) for r in rows]


def open_digest(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> DecisionDigest:
    """Batch open decisions into a digest for the Spokesman (ADR-0006, batched)."""
    items = open_decisions(conn, workstream)
    return DecisionDigest(count=len(items), items=items)
