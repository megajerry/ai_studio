"""Worker full-loop tests — pm.tick → PM → work → Executor + Verifier → merged.

Drives ``run_once`` with an in-memory fake queue (no database) and a real
FilesystemTool (tmp dir), fully keyless. Asserts (a) the whole unified dev/review
loop reaches ``merged`` only after the Verifier (the automated reviewer) passes
(verify→commit), (b) the canonical event sequence is emitted, (c) the tool call
went through the policy ``invoke`` gate and the model call through ``call_model``,
and (d) a verify failure drives ``reviewer_blocked → in_progress`` (bounded
retry) and finally ``abandoned`` — never a silent ``merged``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime.enforce import MemoryEventSink
from runtime.models import Task, TaskStatus, make_event
from runtime.policy import load_policy
from runtime.roles.verifier import VerifyResult
from runtime.scheduler import PM_TICK_TYPE
from runtime.task_state import assert_transition
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
    """In-memory stand-in for the M1 queue that emits the same task.* events and
    enforces the canonical state machine on every transition (like the real DB).
    """

    def __init__(self, sink: MemoryEventSink) -> None:
        self.sink = sink
        self.tasks: dict = {}
        self.order: list = []
        self.heartbeats: list = []

    def _emit(self, t: Task, type_: str, **payload) -> None:
        self.sink.emit(make_event(workstream=t.workstream, type=type_,
                                  task_id=t.id, payload={"status": t.status.value, **payload}))

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None, depends_on=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type,
                 status=TaskStatus.UP_FOR_GRABS, priority=priority, payload=payload or {},
                 created_at=now, updated_at=now, budget_tokens=budget_tokens,
                 depends_on=list(depends_on or []))
        self.tasks[t.id] = t
        self.order.append(t.id)
        self._emit(t, "task.created", type=type, priority=priority)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None,
              agent_type=None, sort=None, filter=None):
        # grab (up_for_grabs→claimed) + start (claimed→in_progress), deps must be merged.
        ids = [i for i in self.order
               if self.tasks[i].status is TaskStatus.UP_FOR_GRABS
               and (workstream is None or self.tasks[i].workstream == workstream)
               and all(self.tasks[d].status is TaskStatus.MERGED
                       for d in self.tasks[i].depends_on if d in self.tasks)]
        if not ids:
            return None
        ids.sort(key=lambda i: -self.tasks[i].priority)  # highest priority; stable
        tid = ids[0]
        self.transition(conn, tid, TaskStatus.CLAIMED, agent_id=worker_id,
                        claimed_by=worker_id)
        return self.transition(conn, tid, TaskStatus.IN_PROGRESS, agent_id=worker_id)

    def transition(self, conn, task_id, to, *, agent_id=None, agent_type=None,
                   result=None, spent_tokens=None, expected_from=None,
                   claimed_by=None, **kw):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        assert_transition(t.status, to)  # enforce the canonical machine
        upd = {"status": to}
        if result is not None:
            upd["result"] = result
        if claimed_by is not None:
            upd["claimed_by"] = claimed_by
        t = t.model_copy(update=upd)
        self.tasks[task_id] = t
        self._emit(t, "task.transition",
                   **{"from": None, "to": to.value, "agent_id": agent_id})
        if to in (TaskStatus.MERGED, TaskStatus.ABANDONED):
            self._emit(t, "task.finished", spent_tokens=spent_tokens)
        return t

    def heartbeat(self, conn, task_id, worker_id):
        self.heartbeats.append(task_id)
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[task_id] = t
        return t

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.MERGED,
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
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete,
                enqueue=q.enqueue, transition=q.transition)


def _idx(types: list[str], name: str) -> int:
    return types.index(name)


def test_full_loop_reaches_merged_and_emits_canonical_sequence(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    cfg = load_policy()

    q.enqueue(None, workstream="test", type=PM_TICK_TYPE, payload={"goal": "Operate the studio"})

    # Pass 1: claims pm.tick → PM decomposes + enqueues N>1 work tasks, then merges.
    r1 = run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))
    assert r1 is not None and r1.kind == "pm" and r1.outcome == "done"
    work_items = [t for t in q.tasks.values() if t.type.startswith("work.")]
    assert len(work_items) > 1  # genuine decomposition into multiple work items

    # Subsequent passes: claim each work.* task → Executor + Verifier → merged.
    done_count = 0
    while True:
        r = run_once(None, "w1", sink, registry=reg, config=cfg, **_seams(q))
        if r is None:
            break
        assert r.kind == "work" and r.outcome == "done"
        done_count += 1
    assert done_count == len(work_items)  # every decomposed item reached merged

    # Every work task is MERGED and was verified (verify→commit).
    work = [t for t in q.tasks.values() if t.type.startswith("work.")]
    assert all(t.status is TaskStatus.MERGED for t in work)
    assert all(t.result and t.result["verified"] is True for t in work)

    types = sink.types()
    for required in ("pm.planned", "model.routed", "model.call",
                     "policy.decision", "tool.invoked", "task.transition", "task.finished"):
        assert required in types, f"missing {required} in {types}"

    # Ordering: plan precedes the work tool call which precedes the final merge.
    last_finish = len(types) - 1 - types[::-1].index("task.finished")
    assert _idx(types, "pm.planned") < _idx(types, "tool.invoked")
    assert _idx(types, "tool.invoked") < last_finish
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
    assert sink.types().count("policy.decision") >= len(invoked)


def test_verify_fail_retries_via_reviewer_blocked_then_abandons(tmp_path):
    """When the Verifier fails, the worker drives ready_for_review → reviewer_blocked
    → in_progress (bounded retry) and, once attempts are exhausted, → abandoned —
    never a silent merge. The canonical retry path (ADR-0015) is exercised in one
    run_once pass."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    cfg = load_policy()

    def always_fail(conn, task, result, s, **kw):
        return VerifyResult(passed=False, reason="forced fail")

    task = q.enqueue(None, workstream="test", type="work.demo",
                     payload={"goal": "g", "criterion": "c", "marker": "m", "attempt": 1})
    r = run_once(None, "w1", sink, registry=reg, config=cfg,
                 run_verify=always_fail, max_attempts=2, **_seams(q))
    assert r.outcome == "failed"
    assert q.tasks[task.id].status is TaskStatus.ABANDONED

    # The canonical retry path is visible in the transition trail.
    tos = [e.payload.get("to") for e in sink.events if e.type == "task.transition"]
    assert "ready_for_review" in tos
    assert "reviewer_blocked" in tos
    assert tos.count("in_progress") >= 2   # start + one retry back to in_progress
    assert "abandoned" in tos
    assert "work.retry" in sink.types()


def test_unknown_task_type_is_abandoned_not_dropped(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    q.enqueue(None, workstream="test", type="mystery.thing", payload={})
    r = run_once(None, "w1", sink, registry=reg, config=load_policy(), **_seams(q))
    assert r.kind == "unknown" and r.outcome == "failed"
    task = [t for t in q.tasks.values()][0]
    assert task.status is TaskStatus.ABANDONED


def test_run_once_returns_none_when_queue_empty(tmp_path):
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry(tmp_path)
    assert run_once(None, "w1", sink, registry=reg, config=load_policy(), **_seams(q)) is None
