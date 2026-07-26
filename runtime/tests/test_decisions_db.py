"""Async open-ended decision tests — the park → free-worker → resume loop (ADR-0025).

The open-ended analogue of the binary approval loop (test_approvals_db.py). These
exercise the FULL loop against a real Postgres and SKIP cleanly when none is
reachable (off-host sandbox). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_decisions_db.py

Covered end-to-end (live DB):
- request_decision(dependent_task) parks it `blocked`; the worker is FREED — grab_task
  returns a DIFFERENT up_for_grabs task (the blocked one is not grabbable).
- answer_decision resumes the parked task (blocked → up_for_grabs) + records the
  answer; the resumed task reads it via get_decision.
- events are body-free (question/answer text NOT on the wire — sentinel asserted).
- every park/resume/state write goes through the guarded transition (no ad-hoc UPDATE).
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from runtime import db
from runtime.decisions import (
    STATUS_ANSWERED,
    STATUS_OPEN,
    STATUS_WITHDRAWN,
    DecisionParkError,
    answer_decision,
    get_decision,
    open_decisions,
    open_digest,
    request_decision,
    withdraw_decision,
)
from runtime.enforce import DbEventSink
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import TaskStatus
from runtime.tasks import (
    claim_task,
    enqueue_task,
    get_task,
    grab_task,
    rekick_task,
    task_lifecycle,
)

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0015 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-dec-{uuid4().hex[:10]}"


def _in_progress_task(conn, ws, worker_id="dec-w"):
    """Enqueue a task and drive it to in_progress (claimable target to park)."""
    enqueue_task(conn, workstream=ws, type="work.demo", payload={"goal": "g"})
    return claim_task(conn, worker_id=worker_id, workstream=ws)


# --- request_decision: parks the dependent + FREES the worker ----------------


def test_request_decision_parks_task_and_frees_worker(conn, ws):
    sink = DbEventSink(conn)
    # T is the task that needs the decision (in_progress → will be parked).
    t = _in_progress_task(conn, ws)
    assert t is not None and t.status is TaskStatus.IN_PROGRESS
    # T2 is other, independent work the worker should be able to grab meanwhile.
    t2 = enqueue_task(conn, workstream=ws, type="work.other", payload={"goal": "g2"})

    d = request_decision(
        conn, workstream=ws, question="Which vendor: A or B?",
        options=["A", "B"], dependent_task_id=t.id, default_choice="A", sink=sink,
    )
    assert d.status == STATUS_OPEN and d.dependent_task_id == t.id and d.seq is not None

    # T is parked blocked (not grabbable); its result links the decision.
    parked = get_task(conn, t.id)
    assert parked.status is TaskStatus.BLOCKED
    assert parked.result["blocked_on_decision"] == str(d.id)
    assert parked.result["reason"] == "awaiting_decision"

    # THE WORKER IS FREED: grab returns the OTHER task, never the blocked one.
    grabbed = grab_task(conn, worker_id="free-w", workstream=ws)
    assert grabbed is not None
    assert grabbed.id == t2.id  # got the up_for_grabs sibling
    assert grabbed.id != t.id   # the blocked decision task is NOT grabbable


def test_blocked_decision_task_is_not_grabbable_when_alone(conn, ws):
    """With ONLY the parked task present, a grab returns nothing (it's not stalled —
    the worker simply has no work here; it never grabs the blocked task)."""
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    request_decision(conn, workstream=ws, question="q?", dependent_task_id=t.id, sink=sink)
    assert grab_task(conn, worker_id="w", workstream=ws) is None


# --- answer_decision: resumes the parked task + records the answer ------------


def test_answer_resumes_task_and_answer_is_readable(conn, ws):
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    d = request_decision(
        conn, workstream=ws, question="Tone for the post?",
        dependent_task_id=t.id, sink=sink,  # options=None ⇒ free text
    )
    assert get_task(conn, t.id).status is TaskStatus.BLOCKED

    answered = answer_decision(conn, d.id, "warm and concise", "tester", sink)
    assert answered is not None and answered.status == STATUS_ANSWERED
    assert answered.answer == "warm and concise" and answered.answered_by == "tester"
    assert answered.answered_at is not None

    # The parked task is RESUMED (blocked → up_for_grabs), claim cleared.
    resumed = get_task(conn, t.id)
    assert resumed.status is TaskStatus.UP_FOR_GRABS and resumed.claimed_by is None

    # A fresh worker re-grabs it and can READ the chosen answer.
    regrab = grab_task(conn, worker_id="resume-w", workstream=ws)
    assert regrab is not None and regrab.id == t.id
    seen = get_decision(conn, d.id)
    assert seen.answer == "warm and concise" and seen.status == STATUS_ANSWERED


def test_answer_is_guarded_to_open(conn, ws):
    sink = DbEventSink(conn)
    d = request_decision(conn, workstream=ws, question="q?", sink=sink)
    assert answer_decision(conn, d.id, "first", "t", sink) is not None
    # Already answered → cannot re-answer (guarded to open).
    assert answer_decision(conn, d.id, "second", "t", sink) is None
    assert get_decision(conn, d.id).answer == "first"


def test_answer_missing_returns_none(conn, ws):
    assert answer_decision(conn, uuid4(), "x", "t", DbEventSink(conn)) is None


def test_request_without_dependent_touches_no_task(conn, ws):
    sink = DbEventSink(conn)
    d = request_decision(conn, workstream=ws, question="strategy?", sink=sink)
    assert d.dependent_task_id is None and d.status == STATUS_OPEN
    # Answer resolves cleanly with no task to resume.
    answered = answer_decision(conn, d.id, "pivot", "t", sink)
    assert answered is not None and answered.status == STATUS_ANSWERED


# --- read helpers ------------------------------------------------------------


def test_open_decisions_and_digest_scope_by_workstream(conn, ws):
    sink = DbEventSink(conn)
    for i in range(3):
        request_decision(conn, workstream=ws, question=f"q{i}?", sink=sink)
    other = request_decision(conn, workstream=f"{ws}-other", question="q?", sink=sink)

    scoped = open_decisions(conn, ws)
    assert len(scoped) == 3 and all(x.workstream == ws for x in scoped)
    assert other.id not in {x.id for x in scoped}
    # oldest-first by monotonic seq.
    assert [x.seq for x in scoped] == sorted(x.seq for x in scoped)

    digest = open_digest(conn, ws)
    assert digest.count == 3 and len(digest.items) == 3


def test_withdraw_resumes_task_and_leaves_default(conn, ws):
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    d = request_decision(
        conn, workstream=ws, question="q?", dependent_task_id=t.id,
        default_choice="safe", sink=sink,
    )
    assert get_task(conn, t.id).status is TaskStatus.BLOCKED
    wd = withdraw_decision(conn, d.id, "tester", sink)
    assert wd is not None and wd.status == STATUS_WITHDRAWN
    # The parked task is still resumed so it's never orphaned; default is readable.
    assert get_task(conn, t.id).status is TaskStatus.UP_FOR_GRABS
    assert get_decision(conn, d.id).default_choice == "safe"


# --- body-free events (invariants 5 & 6) -------------------------------------


def test_events_carry_identity_not_question_or_answer_text(conn, ws):
    sink = DbEventSink(conn)
    QUESTION = "DO_NOT_LOG_THIS_QUESTION_xyz"
    ANSWER = "DO_NOT_LOG_THIS_ANSWER_xyz"
    OPTION = "DO_NOT_LOG_THIS_OPTION_xyz"
    t = _in_progress_task(conn, ws)
    d = request_decision(
        conn, workstream=ws, question=QUESTION, options=[OPTION, "B"],
        dependent_task_id=t.id, default_choice=OPTION, sink=sink,
    )
    answer_decision(conn, d.id, ANSWER, "tester", sink)

    events = read_events(conn, workstream=ws)
    req = next(e for e in events if e.type == "decision.requested")
    ans = next(e for e in events if e.type == "decision.answered")
    for ev in (req, ans):
        blob = str(ev.payload)
        assert QUESTION not in blob and ANSWER not in blob and OPTION not in blob
        assert "question" not in ev.payload and "answer" not in ev.payload
    # But identity + shape IS present for auditing.
    assert req.payload["decision_id"] == str(d.id)
    assert req.payload["has_options"] is True and req.payload["has_default"] is True
    assert req.payload["seq"] == d.seq
    assert ans.payload["status"] == "answered" and ans.payload["answered_by"] == "tester"


# --- atomic park+record: abort if the park fails (the concurrency defect) ----


def _open_decisions_for(conn, task_id) -> int:
    """Count `open` decisions whose dependent task is ``task_id`` (invariant probe)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM decisions "
            "WHERE dependent_task_id = %s AND status = 'open'",
            (task_id,),
        )
        n = cur.fetchone()["n"]
    conn.commit()
    return n


def test_request_decision_aborts_when_task_not_in_progress(conn, ws):
    """Deterministic repro: a task re-kicked OUT of in_progress cannot be parked, so
    request_decision ABORTS (DecisionParkError) and NO orphan `open` decision is
    committed — never an `open` decision paired with a runnable (un-parked) task."""
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    # Supervisor re-kicks the (apparently stale) task: in_progress → up_for_grabs.
    rekick_task(conn, t.id, made_progress=False)
    assert get_task(conn, t.id).status is TaskStatus.UP_FOR_GRABS

    before = open_decisions(conn, ws)
    with pytest.raises(DecisionParkError):
        request_decision(
            conn, workstream=ws, question="which vendor?",
            dependent_task_id=t.id, sink=sink,
        )
    conn.commit()  # the aborted transaction rolled back; end it cleanly

    # No decision was created, and the task is untouched (still runnable, no open pair).
    assert _open_decisions_for(conn, t.id) == 0
    assert open_decisions(conn, ws) == before  # unchanged (rolled back)
    assert get_task(conn, t.id).status is TaskStatus.UP_FOR_GRABS


def test_rekick_does_not_clobber_a_decision_parked_task(conn, ws):
    """The other half of the race: once request_decision PARKED a task `blocked`, a
    (racing) re-kick must NOT move it back to up_for_grabs — that would strand the
    `open` decision. rekick_task is guarded to claimed/in_progress and no-ops here."""
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    d = request_decision(conn, workstream=ws, question="q?", dependent_task_id=t.id, sink=sink)
    assert get_task(conn, t.id).status is TaskStatus.BLOCKED

    # A re-kick of the now-blocked task is a no-op (not claimed/in_progress).
    assert rekick_task(conn, t.id, made_progress=False) is None
    assert get_task(conn, t.id).status is TaskStatus.BLOCKED  # still parked
    assert get_decision(conn, d.id).status == STATUS_OPEN     # decision preserved

    # The proper resume path still works.
    answered = answer_decision(conn, d.id, "A", "tester", sink)
    assert answered is not None
    assert get_task(conn, t.id).status is TaskStatus.UP_FOR_GRABS


def test_rekick_vs_request_decision_race_is_never_inconsistent(conn):
    """60-trial concurrent rekick-vs-park: NEVER an `open` decision + runnable task.

    Each trial races a supervisor re-kick against request_decision on the SAME
    in_progress task, in fresh per-thread connections. The post-condition holds
    every time: EITHER the decision was not created (park aborted) OR the task is
    `blocked` — never `open` decision paired with a non-blocked task."""
    trials = 60
    inconsistent = 0
    for _ in range(trials):
        setup = db.connect()
        ws = f"race-{uuid4().hex[:10]}"
        enqueue_task(setup, workstream=ws, type="work.demo", payload={"goal": "g"})
        t = claim_task(setup, worker_id="race-w", workstream=ws)
        setup.commit()
        tid = t.id
        setup.close()

        barrier = threading.Barrier(2)

        def do_rekick():
            c = db.connect()
            try:
                barrier.wait()
                try:
                    rekick_task(c, tid, made_progress=False)
                except Exception:
                    pass
                c.commit()
            finally:
                c.close()

        def do_request():
            c = db.connect()
            try:
                barrier.wait()
                try:
                    request_decision(
                        c, workstream=ws, question="q?",
                        dependent_task_id=tid, sink=DbEventSink(c),
                    )
                except DecisionParkError:
                    pass
                c.commit()
            finally:
                c.close()

        threads = [threading.Thread(target=do_rekick), threading.Thread(target=do_request)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        check = db.connect()
        status = get_task(check, tid).status
        open_pairs = _open_decisions_for(check, tid)
        check.close()
        if open_pairs and status is not TaskStatus.BLOCKED:
            inconsistent += 1

    assert inconsistent == 0, f"{inconsistent}/{trials} trials left an open decision on a runnable task"


# --- guarded-writer discipline ----------------------------------------------


def test_park_and_resume_go_through_the_guarded_transition(conn, ws):
    """Every task state change (park + resume) is recorded in task_transitions —
    proof it flowed through runtime.tasks.transition, not an ad-hoc status UPDATE."""
    sink = DbEventSink(conn)
    t = _in_progress_task(conn, ws)
    d = request_decision(conn, workstream=ws, question="q?", dependent_task_id=t.id, sink=sink)
    answer_decision(conn, d.id, "ans", "tester", sink)

    hops = [(h["from_status"], h["to_status"]) for h in task_lifecycle(conn, t.id)["transitions"]]
    # The canonical guarded edges appear: in_progress→blocked (park), blocked→up_for_grabs (resume).
    assert ("in_progress", "blocked") in hops
    assert ("blocked", "up_for_grabs") in hops
