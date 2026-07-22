"""Retro role + learning-loop wiring tests (ADR-0003).

Pure-logic tests (lesson distillation, bounds) and worker-wiring tests (retro
trigger policy, NO retro-loop) run with NO database via an in-memory fake queue.
The live-DB tests exercise ``run_retro`` end-to-end — read the trail, store a
lesson in Knowledge memory, emit ``retro.completed`` with NO lesson text — and
SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.enforce import MemoryEventSink
from runtime.events import append_event, read_events
from runtime.memory import recall_lessons
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.policy import load_policy
from runtime.roles.retro import (
    EVENT_RETRO_COMPLETED,
    MAX_LESSONS,
    RETRO_TASK_TYPE,
    RetroResult,
    distill_lessons,
    run_retro,
)
from runtime.roles.verifier import VerifyResult
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import EVENT_RETRO_TRIGGERED, run_once


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.delenv("WORKER_RETRO", raising=False)

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


# ===========================================================================
# Pure logic — lesson distillation (no DB, no model)
# ===========================================================================


def test_distill_failure_yields_prevention_lesson():
    lessons = distill_lessons(
        ["executor.acted", "verify.failed", "task.finished"],
        outcome="failed", target_task_type="work.demo",
    )
    assert len(lessons) >= 1
    assert any("first attempt" in l for l in lessons)


def test_distill_success_yields_what_worked_lesson():
    lessons = distill_lessons(
        ["executor.acted", "verify.passed", "task.finished"],
        outcome="done", target_task_type="work.demo",
    )
    assert len(lessons) == 1
    assert "passed verification on the first attempt" in lessons[0]


def test_distill_is_bounded():
    # Even a noisy trail never exceeds the cap; reflection is bounded (ADR-0003).
    trail = ["verify.failed"] * 10 + ["work.retry"] * 10
    assert len(distill_lessons(trail, outcome="failed", target_task_type="work.demo")) <= MAX_LESSONS
    assert len(distill_lessons(trail, outcome="failed", target_task_type="work.demo", max_lessons=1)) == 1


def test_distill_never_empty():
    assert distill_lessons([], outcome="done", target_task_type="work.demo")


# ===========================================================================
# Worker wiring — retro trigger policy + NO retro-loop (fake queue, no DB)
# ===========================================================================


class FakeQueue:
    """Minimal in-memory queue mirroring the real enqueue/claim/heartbeat/complete."""

    def __init__(self, sink: MemoryEventSink) -> None:
        self.sink = sink
        self.tasks: dict = {}
        self.order: list = []

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type, status=TaskStatus.QUEUED,
                 priority=priority, payload=payload or {}, created_at=now, updated_at=now)
        self.tasks[t.id] = t
        self.order.append(t.id)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order if self.tasks[i].status is TaskStatus.QUEUED
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        ids.sort(key=lambda i: -self.tasks[i].priority)
        t = self.tasks[ids[0]].model_copy(update={
            "status": TaskStatus.IN_PROGRESS, "claimed_by": worker_id,
            "heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[t.id] = t
        return t

    def heartbeat(self, conn, task_id, worker_id):
        return self.tasks.get(task_id)

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.DONE,
                 spent_tokens=None, force=False):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"status": status, "result": result})
        self.tasks[task_id] = t
        return t

    def queued_of_type(self, type_: str) -> list:
        return [t for t in self.tasks.values()
                if t.type == type_ and t.status is TaskStatus.QUEUED]


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete, enqueue=q.enqueue)


def _pass(conn, task, result, s, **kw):
    return VerifyResult(passed=True, reason="ok")


def _fail(conn, task, result, s, **kw):
    return VerifyResult(passed=False, reason="forced fail")


def test_on_fail_triggers_retro_on_failed(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_fail, max_attempts=1, retro_mode="on_fail", **_seams(q))
    assert r.outcome == "failed"
    assert EVENT_RETRO_TRIGGERED in sink.types()
    assert len(q.queued_of_type(RETRO_TASK_TYPE)) == 1


def test_on_fail_does_not_trigger_retro_on_done(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_pass, retro_mode="on_fail", **_seams(q))
    assert r.outcome == "done"
    assert EVENT_RETRO_TRIGGERED not in sink.types()
    assert q.queued_of_type(RETRO_TASK_TYPE) == []


def test_always_triggers_retro_on_done(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_verify=_pass, retro_mode="always", **_seams(q))
    assert r.outcome == "done"
    assert len(q.queued_of_type(RETRO_TASK_TYPE)) == 1


def test_off_never_triggers_retro(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
             run_verify=_fail, max_attempts=1, retro_mode="off", **_seams(q))
    assert q.queued_of_type(RETRO_TASK_TYPE) == []


def test_no_retro_loop_a_retro_task_enqueues_nothing(tmp_path):
    """Dispatching a retro task must NOT enqueue another task (no retro-of-retro)."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    q.enqueue(None, workstream="t", type=RETRO_TASK_TYPE,
              payload={"target_task_id": str(uuid4()), "target_task_type": "work.demo",
                       "outcome": "failed"})
    before = len(q.tasks)

    def fake_run_retro(conn, task, s, **kw):
        return RetroResult(target_task_id="x", target_task_type="work.demo",
                           outcome="failed", lessons_count=2)

    r = run_once(None, "w1", sink, registry=_registry(tmp_path), config=load_policy(),
                 run_retro=fake_run_retro, retro_mode="always", **_seams(q))
    assert r is not None and r.kind == "retro" and r.outcome == "done"
    # No new task was enqueued by the retro; the retro task itself is done.
    assert len(q.tasks) == before
    assert EVENT_RETRO_TRIGGERED not in sink.types()
    assert q.queued_of_type(RETRO_TASK_TYPE) == []


# ===========================================================================
# Live DB — run_retro reads the trail, stores a lesson, emits no text
# ===========================================================================

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


def _retro_task(ws: str, target_id, outcome: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type=RETRO_TASK_TYPE, status=TaskStatus.IN_PROGRESS,
                priority=0, created_at=now, updated_at=now,
                payload={"target_task_id": str(target_id), "target_task_type": "work.demo",
                         "outcome": outcome})


@pytestmark_db
def test_run_retro_reads_trail_and_stores_lesson(conn):
    ws = f"retro-{uuid4().hex[:12]}"
    target_id = uuid4()
    # Simulate a failed episode's trail for the target task.
    for typ in ("executor.acted", "verify.failed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id,
                                       payload={"note": "x"}))

    sink = MemoryEventSink()
    result = run_retro(conn, _retro_task(ws, target_id, "failed"), sink)

    assert isinstance(result, RetroResult)
    assert result.lessons_count >= 1
    # The lesson is now recallable from Knowledge memory for this workstream.
    lessons = recall_lessons(conn, ws, "verification success criterion marker", k=5)
    assert len(lessons) >= 1


@pytestmark_db
def test_retro_completed_event_carries_no_lesson_text(conn):
    ws = f"retro-{uuid4().hex[:12]}"
    target_id = uuid4()
    for typ in ("executor.acted", "verify.failed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id))

    sink = MemoryEventSink()
    run_retro(conn, _retro_task(ws, target_id, "failed"), sink)

    completed = [e for e in sink.events if e.type == EVENT_RETRO_COMPLETED]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["lessons_count"] >= 1
    assert payload["target_task_type"] == "work.demo"
    # The stored lesson text must NOT appear anywhere in the event payload —
    # the event carries the COUNT + task ref only (invariants 5 & 6).
    lessons = recall_lessons(conn, ws, "verification marker", k=5)
    assert lessons  # a lesson was stored
    for item in lessons:
        assert item.text not in str(payload)
    assert set(payload) == {"target_task_id", "target_task_type", "outcome", "lessons_count"}


@pytestmark_db
def test_run_retro_enqueues_nothing_live(conn):
    """A live retro stores lessons but never touches the task queue (no loop)."""
    ws = f"retro-{uuid4().hex[:12]}"
    target_id = uuid4()
    append_event(conn, make_event(workstream=ws, type="verify.passed", task_id=target_id))
    sink = MemoryEventSink()
    run_retro(conn, _retro_task(ws, target_id, "done"), sink)
    # No task.created event was emitted by the retro (it enqueues nothing).
    assert "task.created" not in sink.types()
