"""Cross-workstream request contract — pure + live-DB tests.

Verticals coordinate ONLY through the task board + event log (never direct
calls): a typed :class:`FeatureRequest` is filed onto the receiving workstream's
board and its PM triages it through its own success lens. Pure tests (contract +
event hygiene) run anywhere; the rest exercise the FULL loop against a real
Postgres and SKIP cleanly when none is reachable. Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_crossworkstream_db.py

Covered:
- submit → a `feature_request` task exists (up_for_grabs) for the RECEIVER only.
- accept → decompose into N up_for_grabs work items carrying the requester's
  criteria + a back-link, + `request.accepted`; the request task is merged.
- decline → `request.declined` (reason) and NO work; the request is abandoned.
- needs_clarification → `request.needs_clarification` back; request re-queued.
- escalate → `request.escalated` + a real 🛑 approval row; request parked blocked.
- the requester observes the outcome purely via the `request.*` event stream.
- scope respected: a request to B is not listed/processable as A's.
- events leak NO request bodies (problem / desired_capability / criteria).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from runtime import db
from runtime.approvals import STATUS_PENDING, get_approval
from runtime.crossworkstream import (
    EVENT_REQUEST_ACCEPTED,
    EVENT_REQUEST_DECLINED,
    EVENT_REQUEST_ESCALATED,
    EVENT_REQUEST_NEEDS_CLARIFICATION,
    EVENT_REQUEST_SUBMITTED,
    EVENT_REQUEST_UNDER_REVIEW,
    FEATURE_REQUEST_TYPE,
    STATUS_ACCEPTED,
    STATUS_DECLINED,
    STATUS_ESCALATED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_SUBMITTED,
    FeatureRequest,
    emit_request_event,
    get_request,
    list_requests,
    request_status,
    submit_request,
)
from runtime.enforce import DbEventSink, MemoryEventSink
from runtime.events import read_events
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus
from runtime.roles.pm import triage_request

# --- distinctive secret bodies used to prove events never leak them ----------
SECRET_PROBLEM = "SECRET-PROBLEM-xyzzy-do-not-leak"
SECRET_CAPABILITY = "SECRET-CAPABILITY-plugh-do-not-leak"
SECRET_CRITERION = "SECRET-CRITERION-frobnitz-do-not-leak"


def _make_request(from_ws: str, to_ws: str, **over) -> FeatureRequest:
    fields = dict(
        from_workstream=from_ws,
        to_workstream=to_ws,
        title="Add a video audit capability",
        problem=SECRET_PROBLEM,
        desired_capability=SECRET_CAPABILITY,
        success_criteria=[SECRET_CRITERION, "duration under 60s"],
        impact="unblocks the productivity vertical's launch",
        priority=5,
    )
    fields.update(over)
    return FeatureRequest(**fields)


# --- pure (no DB) -----------------------------------------------------------


def test_feature_request_roundtrips_through_task_payload():
    req = _make_request("video", "productivity")
    task = Task(
        id=uuid4(), workstream="productivity", type=FEATURE_REQUEST_TYPE,
        status=TaskStatus.UP_FOR_GRABS, priority=5,
        payload={**req.to_payload(), "request_status": STATUS_SUBMITTED,
                 "work_task_ids": ["x"]},
        created_at=__import__("datetime").datetime.now(),
        updated_at=__import__("datetime").datetime.now(),
    )
    back = FeatureRequest.from_task(task)
    assert back == req  # sub-status / bookkeeping keys stripped on the way back


def test_from_task_rejects_wrong_type():
    task = Task(
        id=uuid4(), workstream="productivity", type="work.task",
        status=TaskStatus.UP_FOR_GRABS, priority=0, payload={},
        created_at=__import__("datetime").datetime.now(),
        updated_at=__import__("datetime").datetime.now(),
    )
    with pytest.raises(ValueError):
        FeatureRequest.from_task(task)


def test_emit_request_event_carries_no_bodies():
    sink = MemoryEventSink()
    rid = uuid4()
    emit_request_event(
        sink, type=EVENT_REQUEST_ACCEPTED, request_id=rid,
        from_workstream="video", to_workstream="productivity",
        status=STATUS_ACCEPTED, decision=STATUS_ACCEPTED,
        reason="fits our roadmap", work_item_count=2, work_task_ids=["a", "b"],
    )
    (ev,) = sink.events
    assert ev.workstream == "productivity"  # scoped to the receiver's board
    assert ev.payload["request_id"] == str(rid)
    assert ev.payload["from_workstream"] == "video"
    assert ev.payload["decision"] == STATUS_ACCEPTED
    blob = json.dumps(ev.payload)
    for secret in (SECRET_PROBLEM, SECRET_CAPABILITY, SECRET_CRITERION):
        assert secret not in blob


# --- live DB ----------------------------------------------------------------

pytestmark_db = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def wss() -> tuple[str, str]:
    tag = uuid4().hex[:10]
    return (f"from-{tag}", f"to-{tag}")


def _status(conn, task_id) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (task_id,))
        s = cur.fetchone()["status"]
    conn.commit()
    return s


def _no_bodies_leaked(conn, request_id) -> None:
    """Assert NO request body text appears in ANY event for this request."""
    events = read_events(conn, task_id=request_id)
    for ev in events:
        blob = json.dumps(ev.payload)
        for secret in (SECRET_PROBLEM, SECRET_CAPABILITY, SECRET_CRITERION):
            assert secret not in blob, f"{secret!r} leaked into {ev.type}"


@pytestmark_db
def test_submit_creates_request_task_for_receiver(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=sink)

    assert task.type == FEATURE_REQUEST_TYPE
    assert task.workstream == to_ws  # scoped to the RECEIVER's board
    assert task.status is TaskStatus.UP_FOR_GRABS
    assert request_status(task) == STATUS_SUBMITTED

    types = [e.type for e in read_events(conn, task_id=task.id)]
    assert EVENT_REQUEST_SUBMITTED in types
    _no_bodies_leaked(conn, task.id)


@pytestmark_db
def test_scope_respected_request_to_b_not_seen_by_a(conn, wss):
    from_ws, to_ws = wss
    other = f"other-{uuid4().hex[:8]}"
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=DbEventSink(conn))

    assert task.id in {t.id for t in list_requests(conn, to_ws)}
    assert task.id not in {t.id for t in list_requests(conn, other)}
    # A submitted-only filter still finds it for the receiver.
    assert task.id in {t.id for t in list_requests(conn, to_ws, status=STATUS_SUBMITTED)}
    # A PM for another workstream may not triage a request addressed elsewhere.
    with pytest.raises(ValueError):
        triage_request(conn, task, DbEventSink(conn), receiving_workstream=other)


@pytestmark_db
def test_accept_decomposes_with_requester_criteria(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=sink)

    result = triage_request(conn, task, sink, receiving_workstream=to_ws)
    assert result.decision == STATUS_ACCEPTED
    assert result.work_item_count == 2  # one per success criterion

    # The created work items are up_for_grabs in the RECEIVER's workstream and
    # carry the requester's criteria + a back-link to the request.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, workstream, status, type, payload FROM tasks WHERE id = ANY(%s)",
            ([UUID(i) for i in result.work_task_ids],),
        )
        rows = cur.fetchall()
    conn.commit()
    assert len(rows) == 2
    criteria = set()
    for r in rows:
        assert r["workstream"] == to_ws
        assert r["status"] == "up_for_grabs"
        assert r["type"].startswith("work.")
        assert r["payload"]["request_id"] == str(task.id)
        assert r["payload"]["from_workstream"] == from_ws
        criteria.add(r["payload"]["criterion"])
    assert SECRET_CRITERION in criteria  # requester's criterion became the item's

    types = [e.type for e in read_events(conn, task_id=task.id)]
    assert EVENT_REQUEST_UNDER_REVIEW in types
    assert EVENT_REQUEST_ACCEPTED in types
    # The accepted request itself is driven to a terminal (merged) state.
    assert _status(conn, task.id) == "merged"
    _no_bodies_leaked(conn, task.id)


@pytestmark_db
def test_decline_records_reason_and_enqueues_no_work(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=sink)

    before = _work_count(conn, to_ws)
    result = triage_request(
        conn, task, sink, decision="decline",
        reason="out of scope for our roadmap this quarter",
        receiving_workstream=to_ws,
    )
    assert result.decision == STATUS_DECLINED
    assert result.work_item_count == 0
    assert _work_count(conn, to_ws) == before  # NO work enqueued

    declined = [e for e in read_events(conn, task_id=task.id)
                if e.type == EVENT_REQUEST_DECLINED]
    assert declined and declined[0].payload["reason"].startswith("out of scope")
    assert _status(conn, task.id) == "abandoned"
    _no_bodies_leaked(conn, task.id)


@pytestmark_db
def test_needs_clarification_goes_back_and_requeues(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    # A request with nothing checkable → the keyless default asks for clarification.
    thin = _make_request(from_ws, to_ws, success_criteria=[], desired_capability="")
    task = submit_request(conn, request=thin, sink=sink)

    result = triage_request(conn, task, sink, receiving_workstream=to_ws)
    assert result.decision == STATUS_NEEDS_CLARIFICATION

    types = [e.type for e in read_events(conn, task_id=task.id)]
    assert EVENT_REQUEST_NEEDS_CLARIFICATION in types
    # Returned to the board so a clarified re-submission can be re-triaged.
    assert _status(conn, task.id) == "up_for_grabs"
    assert request_status(get_request(conn, task.id)) == STATUS_NEEDS_CLARIFICATION


@pytestmark_db
def test_escalate_raises_a_real_stop_approval(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=sink)

    result = triage_request(
        conn, task, sink, decision="escalate",
        reason="cross-portfolio resourcing call needed",
        receiving_workstream=to_ws,
    )
    assert result.decision == STATUS_ESCALATED
    assert result.approval_id is not None

    approval = get_approval(conn, UUID(result.approval_id))
    assert approval is not None
    assert approval.tier == "🛑"
    assert approval.status == STATUS_PENDING
    assert approval.task_id == task.id

    types = [e.type for e in read_events(conn, task_id=task.id)]
    assert EVENT_REQUEST_ESCALATED in types
    # The request is parked blocked on the approval (symmetric escalation gate).
    assert _status(conn, task.id) == "blocked"
    _no_bodies_leaked(conn, task.id)


@pytestmark_db
def test_requester_observes_outcome_via_events(conn, wss):
    from_ws, to_ws = wss
    sink = DbEventSink(conn)
    task = submit_request(conn, request=_make_request(from_ws, to_ws), sink=sink)
    triage_request(conn, task, sink, decision="accept", receiving_workstream=to_ws)

    # The requester (from_ws) never calls the receiver directly — it reads the
    # request.* event stream and sees its request move submitted → accepted.
    events = [e for e in read_events(conn, task_id=task.id)
              if e.type.startswith("request.")]
    seen = [e.type for e in events]
    assert seen[0] == EVENT_REQUEST_SUBMITTED
    assert EVENT_REQUEST_ACCEPTED in seen
    # Every request.* event names the requester so it can filter its own stream.
    assert all(e.payload.get("from_workstream") == from_ws for e in events)


def _work_count(conn, workstream: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE workstream = %s AND type LIKE 'work.%%'",
            (workstream,),
        )
        n = cur.fetchone()["n"]
    conn.commit()
    return int(n)
