"""Worker full-loop tests — pm.tick → PM → work → Executor + Verifier → done.

Drives ``run_once`` with an in-memory fake queue (no database) and a real
FilesystemTool (tmp dir), fully keyless. Asserts (a) the whole loop reaches
``done`` only after the Verifier passes (verify→commit), (b) the canonical event
sequence is emitted, (c) the tool call went through the policy ``invoke`` gate and
the model call through ``call_model``, and (d) a verify failure triggers a bounded
re-enqueue rather than a silent ``done``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime.enforce import MemoryEventSink
from runtime.models import Task, TaskStatus
from runtime.policy import load_policy
from runtime.roles.verifier import VerifyResult
from runtime.scheduler import PM_TICK_TYPE
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import run_once


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


class FakeQueue:
    """In-memory stand-in for the M1 queue that emits the same task.* events.

    Mirrors the real signatures of enqueue/claim/heartbeat/complete so the worker
    is exercised unchanged, with no Postgres.
    """

    def __init__(self, sink: MemoryEventSink) -> None:
        self.sink = sink
        self.tasks: dict = {}
        self.order: list = []
        self.heartbeats: list = []

    def _emit(self, t: Task, type_: str, **payload) -> None:
        from runtime.models import make_event

        self.sink.emit(make_event(workstream=t.workstream, type=type_,
                                  task_id=t.id, payload={"status": t.status.value, **payload}))

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type, status=TaskStatus.QUEUED,
                 priority=priority, payload=payload or {}, created_at=now, updated_at=now,
                 budget_tokens=budget_tokens)
        self.tasks[t.id] = t
        self.order.append(t.id)
        self._emit(t, "task.created", type=type, priority=priority)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order
               if self.tasks[i].status is TaskStatus.QUEUED
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        ids.sort(key=lambda i: -self.tasks[i].priority)  # highest priority; stable
        t = self.tasks[ids[0]].model_copy(update={
            "status": TaskStatus.IN_PROGRESS, "claimed_by": worker_id,
            "heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[t.id] = t
        self._emit(t, "task.claimed", claimed_by=worker_id)
        return t

    def heartbeat(self, conn, task_id, worker_id):
        self.heartbeats.append(task_id)
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[task_id] = t
        return t

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.DONE,
                 spent_tokens=None, force=False):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"status": status, "result": result})
        self.tasks[task_id] = t
        self._emit(t, "task.finished", spent_tokens=spent_tokens)
        return t


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete, enqueue=q.enqueue)


def _idx(types: list[str], name: str) -> int:
    return types.index(name)


def test_full_loop_reaches_done_and_emits_canonical_sequence(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    cfg = load_policy()

    q.enqueue(None, workstream="test", type=PM_TICK_TYPE, payload={"goal": "Operate the studio"})

    # Pass 1: claims pm.tick → PM decomposes + enqueues N>1 work tasks, then commits.
    r1 = run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))
    assert r1 is not None and r1.kind == "pm" and r1.outcome == "done"
    work_items = [t for t in q.tasks.values() if t.type.startswith("work.")]
    assert len(work_items) > 1  # genuine decomposition into multiple work items

    # Subsequent passes: claim each work.* task → Executor + Verifier → commit done.
    done_count = 0
    while True:
        r = run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))
        if r is None:
            break
        assert r.kind == "work" and r.outcome == "done"
        done_count += 1
    assert done_count == len(work_items)  # every decomposed item reached done

    # Every work task is DONE and was verified (verify→commit).
    work = [t for t in q.tasks.values() if t.type.startswith("work.")]
    assert all(t.status is TaskStatus.DONE for t in work)
    assert all(t.result and t.result["verified"] is True for t in work)

    types = sink.types()
    # (a) planning happened, (b) both model events, (c) the policy-gated tool
    # call, (d) the task finished — the canonical sequence for one work item.
    for required in ("pm.planned", "model.routed", "model.call",
                     "policy.decision", "tool.invoked", "task.finished"):
        assert required in types, f"missing {required} in {types}"

    # Ordering: plan precedes the work model call which precedes the commit.
    last_finish = len(types) - 1 - types[::-1].index("task.finished")
    assert _idx(types, "pm.planned") < _idx(types, "tool.invoked")
    assert _idx(types, "tool.invoked") < last_finish  # work commit is the final finish
    # Heartbeats fired during work (worker's job, not the role's).
    assert q.heartbeats


def test_tool_call_went_through_invoke_not_direct(tmp_path):
    """The only tool.invoked events carry the filesystem tool + a role — proof the
    side effect went through the policy gate, never a direct execute()."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    cfg = load_policy()
    q.enqueue(None, workstream="test", type=PM_TICK_TYPE, payload={"goal": "x"})
    run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))
    run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))

    invoked = [e for e in sink.events if e.type == "tool.invoked"]
    assert invoked, "expected at least one policy-gated tool invocation"
    for e in invoked:
        assert e.payload["tool"] == "filesystem"
        assert e.payload["role"] in {"executor", "verifier"}
    # Every tool.invoked is preceded by a policy.decision (the gate ran first).
    assert sink.types().count("policy.decision") >= len(invoked)


def test_verify_fail_triggers_bounded_reenqueue(tmp_path):
    """When the Verifier fails, the worker fails the task and re-enqueues a bounded
    retry (attempt+1) rather than marking it done."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    cfg = load_policy()

    def always_fail(conn, task, result, s, **kw):
        return VerifyResult(passed=False, reason="forced fail")

    q.enqueue(None, workstream="test", type="work.demo",
              payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=reg, config=cfg,
                 run_verify=always_fail, max_attempts=2, **_seams(q))
    assert r.outcome == "retry"
    assert "work.retry" in sink.types()

    # Original failed; a fresh attempt-2 work task is queued.
    work_tasks = [t for t in q.tasks.values() if t.type == "work.demo"]
    assert any(t.status is TaskStatus.FAILED for t in work_tasks)
    retry = [t for t in work_tasks if t.status is TaskStatus.QUEUED][0]
    assert retry.payload["attempt"] == 2

    # Second run on the retry (still failing) exhausts the bound → failed.
    r2 = run_once(None, "w1", sink, registry=reg, config=cfg,
                  run_verify=always_fail, max_attempts=2, **_seams(q))
    assert r2.outcome == "failed"


def test_unknown_task_type_is_failed_not_dropped(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    q.enqueue(None, workstream="test", type="mystery.thing", payload={})
    r = run_once(None, "w1", sink, registry=reg, config=load_policy(), **_seams(q))
    assert r.kind == "unknown" and r.outcome == "failed"
    task = [t for t in q.tasks.values()][0]
    assert task.status is TaskStatus.FAILED


def test_run_once_returns_none_when_queue_empty(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    assert run_once(None, "w1", sink, registry=reg, config=load_policy(), **_seams(q)) is None
