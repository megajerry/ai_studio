"""Live-DB tests for the free-form training-data store (ADR-0032).

Prove the body-free reconciliation: several emit sites used to embed free-form text
(PM ``goal`` / ``reason``, model-authored verifier ``verdict.reason``) directly in
``events.payload``. That text is now RELOCATED to a dedicated local store
(``event_free_form``, migration 0019) via the single guarded writer
:mod:`runtime.free_form`, and the event log stays BODY-FREE (invariant #6).

SKIP cleanly when no disposable DB is reachable (off-host / no docker). Run:

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    export AI_STUDIO_TEST_DB=1
    python -m runtime.migrate
    pytest runtime/tests/test_free_form_db.py

Covered:
- guarded writer/reader roundtrip + degrade-safety (no conn / blank / unknown kind);
- body-free SENTINEL — after a real pm.tick (pushback, needs_clarification, planned,
  consensus) + a real verify, NO relocated free-text appears in ANY events.payload;
- the store HAS the text, retrievable by kind + task;
- linkage — every free-form row ties back to its task + event_type (+ trajectory);
- consumer coupling — the spokesman digest for the pushback still renders (it reads
  the bounded approvals.reason, not the relocated pm.pushback payload) and the
  quality report still counts verify.passed/failed (which are counts, not reason).
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from runtime import db
from runtime.enforce import DbEventSink
from runtime.events import read_events
from runtime.free_form import KINDS, read_free_form, record_free_form
from runtime.migrate import migrate
from runtime.models import make_event
from runtime.scheduler import PM_TICK_TYPE, tick_once
from runtime.tasks import claim_task

pytestmark = pytest.mark.skipif(
    not db.can_connect(timeout=2.0),
    reason="no reachable DATABASE_URL (expected off-host / no docker)",
)

# The free-text markers we plant; if any leaks onto the wire the sentinel catches it.
GOAL_TEXT = "SENTINEL-GOAL delete the entire production database now"
INFEASIBLE_REASON = "SENTINEL-REASON destructive and utterly out of scope"


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    migrate(c)  # ensure 0019 applied
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def ws() -> str:
    return f"test-freeform-{uuid4().hex[:10]}"


# --- guarded writer / reader ------------------------------------------------


def test_record_and_read_roundtrip(conn, ws):
    tid = uuid4()
    task_id = uuid4()
    rid = record_free_form(
        conn, kind="goal", content=GOAL_TEXT, event_type="pm.planned",
        workstream=ws, task_id=task_id, trajectory_id=tid,
    )
    assert rid is not None
    rows = read_free_form(conn, kind="goal", workstream=ws)
    assert len(rows) == 1
    row = rows[0]
    assert row.content == GOAL_TEXT
    assert row.event_type == "pm.planned"
    assert row.task_id == task_id and row.trajectory_id == tid
    assert row.kind in KINDS
    # Retrievable by task linkage too.
    by_task = read_free_form(conn, task_id=task_id)
    assert [r.id for r in by_task] == [rid]


def test_record_degrades_safely(conn, ws):
    # No conn → no store, no raise (unit/fake-queue paths).
    assert record_free_form(None, kind="goal", content="x", event_type="pm.planned",
                            workstream=ws) is None
    # Blank content → nothing stored.
    assert record_free_form(conn, kind="goal", content="   ", event_type="pm.planned",
                            workstream=ws) is None
    assert record_free_form(conn, kind="goal", content=None, event_type="pm.planned",
                            workstream=ws) is None
    # Unknown kind → skipped (not raised) so an emit is never broken.
    assert record_free_form(conn, kind="bogus", content="x", event_type="pm.planned",
                            workstream=ws) is None
    assert read_free_form(conn, workstream=ws) == []


# --- end-to-end body-free sentinel: PM pushback -----------------------------


def _dry_run_env(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")


def _completion(obj: dict):
    return lambda **kw: type("C", (), {"text": json.dumps(obj)})()


#: The ONE documented, intentional exception (ADR-0006 / ADR-0032): the SHORT,
#: bounded approvals.reason stays on ``approval.requested`` — the spokesman digest
#: reads it. The relocation is about the UNBOUNDED free-text on the pm.*/verify.*/
#: work.retry events; this event type is deliberately excluded from the leak scan.
_APPROVALS_REASON_EXCEPTION = "approval.requested"


def _assert_no_freetext_on_wire(conn, ws):
    """No relocated free-text marker appears in any events.payload for the ws, save
    the documented bounded approvals.reason exception (see ADR-0032)."""
    for e in read_events(conn, workstream=ws):
        if e.type == _APPROVALS_REASON_EXCEPTION:
            continue  # bounded, intentional, spokesman-consumed — retained by design
        blob = json.dumps(e.payload)
        assert "SENTINEL-GOAL" not in blob, f"goal leaked on {e.type}: {blob}"
        assert "SENTINEL-REASON" not in blob, f"reason leaked on {e.type}: {blob}"


def test_pm_pushback_relocates_goal_and_reason(conn, ws, monkeypatch):
    _dry_run_env(monkeypatch)
    from runtime.roles.pm import run_pm_tick

    sink = DbEventSink(conn)
    assert tick_once(conn, workstream=ws) is not None
    claimed = claim_task(conn, worker_id="pm", workstream=ws)
    assert claimed is not None

    infeasible = {
        "restated_goal": GOAL_TEXT, "confidence": 0.95, "feasible": False,
        "reason": INFEASIBLE_REASON, "work_items": [],
    }
    # The pm.tick task itself carries the goal in its (task, not event) payload — set
    # it so the PM resolves our sentinel goal.
    claimed.payload = dict(claimed.payload or {}, goal=GOAL_TEXT)
    plan = run_pm_tick(conn, claimed, sink, call_model=_completion(infeasible))
    assert plan.decision == "pushback"
    conn.commit()

    # (a) body-free: the sentinel goal/reason are NOT in any event payload.
    _assert_no_freetext_on_wire(conn, ws)
    # The pm.pushback event exists but carries only body-free fields.
    pushbacks = [e for e in read_events(conn, workstream=ws) if e.type == "pm.pushback"]
    assert pushbacks and set(pushbacks[0].payload) == {"confidence"}

    # (b) the store HAS the relocated text, retrievable by kind + task.
    goals = read_free_form(conn, kind="goal", task_id=claimed.id)
    reasons = read_free_form(conn, kind="reason", task_id=claimed.id)
    assert any(g.content == GOAL_TEXT for g in goals)
    assert any(INFEASIBLE_REASON in r.content for r in reasons)

    # (d) linkage: every relocated row ties back to this task + the pm.pushback type.
    for row in goals + reasons:
        assert row.task_id == claimed.id
        assert row.event_type == "pm.pushback"

    # (c) consumer coupling: the spokesman still renders the pushback approval — it
    # reads the BOUNDED approvals.reason (unaffected), never the pm.pushback payload.
    from spokesman.runtime_bridge import classify_event
    from runtime.event_types import EVENT_APPROVAL_REQUESTED
    appr = [e for e in read_events(conn, workstream=ws)
            if e.type == EVENT_APPROVAL_REQUESTED]
    assert appr, "pushback must raise an approval.requested for the spokesman digest"
    item = classify_event(appr[0], conn)
    assert item is not None and item.text  # digest text still composes


def test_pm_planned_relocates_goal_body_free(conn, ws, monkeypatch):
    _dry_run_env(monkeypatch)
    from runtime.roles.pm import run_pm_tick

    sink = DbEventSink(conn)
    assert tick_once(conn, workstream=ws) is not None
    claimed = claim_task(conn, worker_id="pm", workstream=ws)
    claimed.payload = dict(claimed.payload or {}, goal=GOAL_TEXT)

    plan = run_pm_tick(conn, claimed, sink)  # keyless dry-run planner decomposes
    assert plan.decision == "planned"
    conn.commit()

    _assert_no_freetext_on_wire(conn, ws)
    planned = [e for e in read_events(conn, workstream=ws) if e.type == "pm.planned"]
    assert planned
    # Body-free: ids/counts only, no goal text.
    assert "goal" not in planned[0].payload
    assert set(planned[0].payload) == {"confidence", "work_item_count", "work_task_ids"}
    # The goal was relocated and links back to the pm.tick task.
    goals = read_free_form(conn, kind="goal", task_id=claimed.id, event_type="pm.planned")
    assert any(g.content == GOAL_TEXT for g in goals)


# --- verify.* body-free sentinel + quality-counter coupling -----------------


def test_verify_relocates_reason_body_free(conn, ws):
    """A direct verify() emits a body-free verify.* and relocates verdict.reason as
    'rationale'; the quality report (which COUNTS verify.passed/failed) is unaffected."""
    from runtime.roles.executor import ExecutorResult
    from runtime.roles.verifier import verify
    from runtime.tasks import enqueue_task
    from runtime.tools import FilesystemTool, ToolRegistry
    import tempfile
    import os

    root = tempfile.mkdtemp()
    marker = "studio-ok:ff"
    with open(os.path.join(root, "out.txt"), "w") as f:
        f.write(f"work done {marker}")

    registry = ToolRegistry()
    registry.register(FilesystemTool(root=root))

    task = enqueue_task(
        conn, workstream=ws, type="work.task",
        payload={"goal": GOAL_TEXT, "criterion": f"contains {marker}", "marker": marker},
    )
    result = ExecutorResult(ok=True, artifact_path="out.txt", marker=marker,
                            invoke_status="executed")
    sink = DbEventSink(conn)
    verdict = verify(conn, task, result, sink, registry=registry)
    assert verdict.passed
    conn.commit()

    # Body-free: verify.passed carries only the pass flag; reason is relocated.
    vp = [e for e in read_events(conn, workstream=ws) if e.type == "verify.passed"]
    assert vp and set(vp[0].payload) == {"passed"}
    assert "reason" not in vp[0].payload
    rationale = read_free_form(conn, kind="rationale", task_id=task.id,
                               event_type="verify.passed")
    assert rationale and rationale[0].content == verdict.reason

    # Consumer coupling: quality still counts the verify.passed signal (count, not text).
    from runtime.quality import quality_report
    report = quality_report(conn, workstream=ws)
    assert report["totals"]["verify_passed"] >= 1
