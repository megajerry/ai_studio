"""Runtime bridge — the Spokesman's read/act window onto the live runtime DB.

Architecture §9: the Spokesman "aggregates all-workstream state from the event
log." Until now it only read/wrote the git ``state/`` tree; this module wires it
to the runtime Postgres so 🛑 approvals / 📣 informs / 🚨 alarms reflect *real*
studio state and an inbound reply resolves a *real* approval.

It reuses the runtime's own data-access layer (no schema changes):

- :func:`poll_notifications` reads new events past a monotonic ``seq`` cursor and
  classifies them into ADR-0006 tiers — 🛑 approve (``approval.requested``),
  🚨 alarm (``review.alarm``, immediate), 📣 inform (``task.failed_exhausted`` /
  ``review.flagged``, batched). The cursor is the events ``seq`` (monotonic), so a
  consumer never re-notifies an event it already saw.
- :func:`studio_status` summarizes the task queue + approvals + spend for a
  ``status`` reply.
- :func:`resolve` calls :func:`runtime.approvals.resolve_approval` (approve/deny);
  the worker's ``resume_approved`` then re-queues / fails the blocked task.

**No secret / argument value ever enters a notification.** The runtime already
emits leak-free payloads (ids / role / tool / tier / reason / fact-based signal
reasons + counts — never arg values or secrets, CLAUDE.md invariant 5); this
module composes text from *only* those safe fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import UUID

import psycopg

from runtime.approvals import STATUS_APPROVED, STATUS_DENIED, pending_approvals
from runtime.decisions import get_decision
from runtime.enforce import DbEventSink
from runtime.event_types import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_DECISION_REQUESTED,
    EVENT_REVIEW_ALARM,
    EVENT_REVIEW_FLAGGED,
    EVENT_TASK_FAILED_EXHAUSTED,
)
from runtime.events import read_events
from runtime.models import Event

from .classify import NotifyKind

# --- event → ADR-0006 tier mapping ------------------------------------------

#: 🚨 immediate/interrupt — the genuine few (ADR-0006).
ALARM_EVENT_TYPES = frozenset({EVENT_REVIEW_ALARM})
#: 🛑 approve (blocks) — batched into the periodic digest.
APPROVE_EVENT_TYPES = frozenset({EVENT_APPROVAL_REQUESTED})
#: 🛑 decide (blocks) — an OPEN-ENDED decision that parks a task (ADR-0025); like an
#: approval it needs a human reply, so it is batched into the same periodic digest.
DECISION_EVENT_TYPES = frozenset({EVENT_DECISION_REQUESTED})
#: 📣 inform (non-blocking) — major mistake / recovery, written to the feed.
INFORM_EVENT_TYPES = frozenset({EVENT_TASK_FAILED_EXHAUSTED, EVENT_REVIEW_FLAGGED})

#: How much of a free-form reason string to carry into a message (hygiene; the
#: runtime reasons are leak-free but can be long when signals are concatenated).
MAX_REASON_CHARS = 240

#: Cursor file (git-ignored runtime state — see .gitignore ``state/spokesman/``).
CURSOR_DIR_NAME = "spokesman"
CURSOR_FILE_NAME = "notify-cursor.txt"


def _short(value: object) -> str:
    """First 8 chars of a UUID-ish id, for a compact human-facing reference."""
    s = str(value or "")
    return s[:8] if s else "(none)"


def _reason(payload: dict) -> str:
    """Render a leak-free reason from an event payload (``reason`` or ``reasons``)."""
    reason = payload.get("reason")
    if not reason:
        reasons = payload.get("reasons")
        if isinstance(reasons, (list, tuple)):
            reason = "; ".join(str(r) for r in reasons)
        elif reasons:
            reason = str(reasons)
    reason = (reason or "").strip()
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS].rstrip() + "…"
    return reason


# --- notification items -----------------------------------------------------


@dataclass(frozen=True)
class NotificationItem:
    """One classified, ready-to-send notification derived from an event.

    ``text`` is composed from leak-free payload fields only; ``approval_id`` is
    set for 🛑 approvals so the app can prompt an ``approve/deny <id>`` reply.
    """

    kind: NotifyKind
    text: str
    seq: int
    event_type: str
    task_id: Optional[str] = None
    approval_id: Optional[str] = None
    decision_id: Optional[str] = None


@dataclass
class NotificationBatch:
    """The result of one :func:`poll_notifications` pass.

    ``cursor`` is the new high-water ``seq`` to persist. ``alarms`` are 🚨 items to
    send immediately; ``digest_items`` are 🛑/📣 items to batch (ADR-0006).
    """

    items: list[NotificationItem] = field(default_factory=list)
    cursor: int = 0

    @property
    def alarms(self) -> list[NotificationItem]:
        return [i for i in self.items if i.kind is NotifyKind.ALARM]

    @property
    def digest_items(self) -> list[NotificationItem]:
        return [i for i in self.items if i.kind is not NotifyKind.ALARM]


def _render_decision(
    decision_id: object, payload: dict, conn: Optional[psycopg.Connection]
) -> str:
    """Render a leak-free 🛑 decision prompt: question + options + default + reply.

    The ``decision.requested`` event is body-free (no question text), so the human
    text is composed from the ``decisions`` ROW (read via ``conn`` — only that row's
    own question / options / default_choice, no arg values or secrets). Without a
    ``conn`` (pure classification) it degrades to the body-free payload so the item
    is never dropped. The reply hint drives the inbound ``decide <id> <answer>`` verb.
    """
    short = _short(decision_id)
    question = ""
    options: Optional[list] = None
    default_choice = None
    if conn is not None and decision_id:
        try:
            decision = get_decision(conn, str(decision_id))
        except Exception:  # noqa: BLE001 - a render read must never break the poll
            decision = None
        if decision is not None:
            question = (decision.question or "").strip()
            options = decision.options
            default_choice = decision.default_choice

    if len(question) > MAX_REASON_CHARS:
        question = question[:MAX_REASON_CHARS].rstrip() + "…"

    text = f"Decision needed [{short}]"
    text += f": {question}" if question else (
        " (options)" if payload.get("has_options") else ""
    )
    if options:
        text += "\nOptions: " + " | ".join(str(o) for o in options)
    if default_choice:
        text += f"\nDefault if unanswered: {default_choice}"
    if decision_id:
        text += f"\nReply: decide {decision_id} <answer>"
    return text


def classify_event(
    event: Event, conn: Optional[psycopg.Connection] = None
) -> Optional[NotificationItem]:
    """Classify one runtime event into an ADR-0006 :class:`NotificationItem`, or None.

    Only the event types the stakeholder must see become items; everything else
    (the high-volume operational trail: policy.decision / tool.invoked /
    model.call / task.claimed / …) is intentionally *not* surfaced. A
    ``review.flagged`` at HIGH severity is dropped here because the same episode
    already emits a ``review.alarm`` (🚨) + an ``approval.requested`` (🛑) — this
    avoids a duplicate 📣 for one incident.

    An OPEN-ENDED ``decision.requested`` (ADR-0025) is surfaced 🛑 batched like an
    approval, but its wire event is body-free (no question text), so ``conn`` is used
    to read the question / options / default_choice from the ``decisions`` row for
    rendering (leak-free — only that row's own fields). Without a ``conn`` the item
    still renders from the body-free payload (id + has_options) so it is never lost.
    """
    etype = event.type
    payload = event.payload or {}
    seq = event.seq or 0
    task_id = str(event.task_id) if event.task_id else None

    if etype in ALARM_EVENT_TYPES:
        reason = _reason(payload)
        sev = payload.get("severity", "high")
        count = payload.get("signal_count")
        detail = f" ({count} signal(s))" if count else ""
        text = f"reviewer flagged {sev} risk{detail}" + (f": {reason}" if reason else "")
        return NotificationItem(
            kind=NotifyKind.ALARM, text=text, seq=seq,
            event_type=etype, task_id=task_id,
        )

    if etype in APPROVE_EVENT_TYPES:
        approval_id = payload.get("approval_id")
        role = payload.get("role", "?")
        tool = payload.get("tool", "?")
        tier = payload.get("tier", "?")
        reason = _reason(payload)
        text = (
            f"Approval needed [{_short(approval_id)}]: {role} → {tool} ({tier})"
            + (f" — {reason}" if reason else "")
            + (f"\nReply: approve {approval_id} | deny {approval_id}" if approval_id else "")
        )
        return NotificationItem(
            kind=NotifyKind.APPROVE, text=text, seq=seq, event_type=etype,
            task_id=task_id, approval_id=str(approval_id) if approval_id else None,
        )

    if etype in DECISION_EVENT_TYPES:
        decision_id = payload.get("decision_id")
        text = _render_decision(decision_id, payload, conn)
        return NotificationItem(
            kind=NotifyKind.APPROVE, text=text, seq=seq, event_type=etype,
            task_id=task_id, decision_id=str(decision_id) if decision_id else None,
        )

    if etype in INFORM_EVENT_TYPES:
        if etype == EVENT_REVIEW_FLAGGED and payload.get("severity") == "high":
            return None  # already covered by the 🚨 alarm + 🛑 approval for this episode
        if etype == EVENT_TASK_FAILED_EXHAUSTED:
            retries = payload.get("retries")
            text = (
                f"Task {_short(task_id)} failed after exhausting retries"
                + (f" ({retries})" if retries is not None else "")
            )
        else:  # review.flagged (non-high)
            sev = payload.get("severity", "?")
            count = payload.get("signal_count")
            reason = _reason(payload)
            text = (
                f"Review flagged {sev} risk"
                + (f" ({count} signal(s))" if count else "")
                + (f": {reason}" if reason else "")
            )
        return NotificationItem(
            kind=NotifyKind.INFORM, text=text, seq=seq,
            event_type=etype, task_id=task_id,
        )

    return None


def poll_notifications(
    conn: psycopg.Connection,
    since_cursor: int = 0,
    *,
    limit: Optional[int] = None,
) -> NotificationBatch:
    """Read + classify new events past ``since_cursor`` (the events ``seq``).

    Reads across *all* workstreams (the Spokesman is the single all-workstream
    interface) in append order and returns the classified items plus the new
    high-water ``seq`` cursor. The cursor advances past *every* new event scanned
    — even ones that produced no notification — so operational churn is skipped
    exactly once and never re-scanned. With no new events the cursor is unchanged.
    """
    events = read_events(conn, since_seq=since_cursor, limit=limit)
    batch = NotificationBatch(cursor=since_cursor)
    for event in events:
        if event.seq is not None and event.seq > batch.cursor:
            batch.cursor = event.seq
        item = classify_event(event, conn)
        if item is not None:
            batch.items.append(item)
    return batch


# --- status -----------------------------------------------------------------


@dataclass(frozen=True)
class StudioStatus:
    """A concise, DB-derived snapshot for the ``status`` reply (ADR-0006 feed)."""

    queued: int
    in_progress: int
    blocked: int
    done: int
    failed: int
    pending_approvals: int
    spent_tokens: int

    @property
    def open_tasks(self) -> int:
        return self.queued + self.in_progress

    def render(self) -> str:
        """WhatsApp-friendly one-screen summary."""
        return (
            "*AI Studio status*\n"
            f"• Open tasks: {self.open_tasks} "
            f"(queued {self.queued}, in-progress {self.in_progress})\n"
            f"• Blocked on approval: {self.blocked}\n"
            f"• Pending approvals: {self.pending_approvals}\n"
            f"• Recent failures: {self.failed}\n"
            f"• Done: {self.done}\n"
            f"• Spend: {self.spent_tokens} tokens"
        )


def studio_status(conn: psycopg.Connection) -> StudioStatus:
    """Summarize live task-queue counts + pending approvals + spend from the DB.

    One grouped scan of ``tasks`` (counts + summed ``spent_tokens`` per status)
    plus the pending-approval count. Read-only; carries only aggregates — no task
    payloads, arg values, or secrets.
    """
    counts: dict[str, int] = {}
    spent = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, count(*) AS n, "
            "COALESCE(sum(spent_tokens), 0) AS tokens FROM tasks GROUP BY status"
        )
        for row in cur.fetchall():
            counts[row["status"]] = int(row["n"])
            spent += int(row["tokens"])
    if not conn.autocommit:
        conn.commit()

    # Map the canonical lifecycle states (ADR-0015) onto the stakeholder-facing
    # summary buckets: up_for_grabs = "queued"; claimed/in_progress/ready_for_review/
    # reviewer_blocked/approved = actively "in progress"; merged = done; abandoned =
    # failed; blocked (on a 🔴 approval) stays its own bucket.
    in_flight = (
        counts.get("claimed", 0) + counts.get("in_progress", 0)
        + counts.get("ready_for_review", 0) + counts.get("reviewer_blocked", 0)
        + counts.get("approved", 0)
    )
    return StudioStatus(
        queued=counts.get("up_for_grabs", 0),
        in_progress=in_flight,
        blocked=counts.get("blocked", 0),
        done=counts.get("merged", 0),
        failed=counts.get("abandoned", 0),
        pending_approvals=len(pending_approvals(conn)),
        spent_tokens=spent,
    )


# --- resolve ----------------------------------------------------------------

#: Inbound decision words → the runtime's canonical approval status.
_DECISION_ALIASES = {
    "approve": STATUS_APPROVED,
    "approved": STATUS_APPROVED,
    "yes": STATUS_APPROVED,
    "deny": STATUS_DENIED,
    "denied": STATUS_DENIED,
    "no": STATUS_DENIED,
    "reject": STATUS_DENIED,
}


def normalize_decision(word: str) -> Optional[str]:
    """Map an inbound word (``approve`` / ``deny`` / …) to a canonical status, or None."""
    return _DECISION_ALIASES.get(word.strip().lower())


def resolve(
    conn: psycopg.Connection,
    approval_id: UUID | str,
    decision: str,
    resolver: str,
):
    """Resolve a pending approval (approve/deny) via the runtime approval store.

    ``decision`` accepts either the canonical ``approved``/``denied`` or an inbound
    alias (``approve``/``deny``/``yes``/``no``). Returns the resolved
    :class:`runtime.approvals.Approval`, or ``None`` if the id is unknown / already
    resolved (``resolve_approval`` is guarded to ``pending``). Emits
    ``approval.resolved`` to the live event log; the worker's ``resume_approved``
    then re-queues (approved) or fails (denied) the blocked task — untouched here.
    """
    from runtime.approvals import resolve_approval  # local import: keeps psycopg lazy

    canonical = normalize_decision(decision) or decision
    if canonical not in (STATUS_APPROVED, STATUS_DENIED):
        raise ValueError("decision must be approve/deny (or approved/denied)")
    if isinstance(approval_id, str):
        approval_id = UUID(approval_id)
    return resolve_approval(conn, approval_id, canonical, resolver, DbEventSink(conn))


def answer(
    conn: psycopg.Connection,
    decision_id: UUID | str,
    answer_text: str,
    resolver: str,
):
    """Answer an OPEN-ENDED decision (ADR-0025) via the runtime decision store.

    The async analogue of :func:`resolve`: it records the chosen answer / free text
    (``answer_decision`` is guarded to ``open``) and, inside the same store call,
    RESUMES the parked dependent task (``blocked → up_for_grabs``) so a fresh worker
    re-grabs it and reads the answer. Returns the answered
    :class:`runtime.decisions.Decision`, or ``None`` if the id is unknown / already
    answered. Emits a body-free ``decision.answered`` to the live event log.
    """
    from runtime.decisions import answer_decision  # local import: keeps psycopg lazy

    if isinstance(decision_id, str):
        decision_id = UUID(decision_id)
    return answer_decision(conn, decision_id, answer_text, resolver, DbEventSink(conn))


# --- cursor persistence -----------------------------------------------------


def _cursor_path(state_dir: Path) -> Path:
    return Path(state_dir) / CURSOR_DIR_NAME / CURSOR_FILE_NAME


def load_cursor(state_dir: Path) -> int:
    """Read the persisted notifier cursor (``seq``); 0 if none yet / unreadable."""
    path = _cursor_path(state_dir)
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def save_cursor(state_dir: Path, seq: int) -> Path:
    """Persist the notifier cursor so a restart does not re-notify old events."""
    path = _cursor_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(seq)), encoding="utf-8")
    return path
