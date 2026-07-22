"""Worker coding-worker dispatch — ``work.code`` / ``prototype`` → the coding path.

No Docker, no opencode, no DB: drives ``run_once`` / ``_handle_code`` with the
in-memory FakeQueue (reused from ``test_worker``) and a stub ``invoke``. Asserts
(a) ``work.code`` and ``prototype`` route to the loop-free coding path (kind
``code``), NOT the generic ``work.*`` dev/review loop; (b) a 🔴 approval PEND parks
the task ``blocked`` (resumed later by the existing approval flow); (c) a granted
EXECUTED dispatch merges on worker success and abandons on worker failure; and
(d) a policy DENY abandons the task.
"""

from __future__ import annotations

import httpx
import pytest

from runtime.enforce import InvokeResult, InvokeStatus, MemoryEventSink
from runtime.models import TaskStatus
from runtime.policy import Decision, Effect, load_policy
from runtime.capabilities import ActionTier
from runtime.tools import CodingTool, ToolRegistry
from runtime.tools.base import ToolResult
from runtime.worker import _handle_code, run_once

from runtime.tests.test_worker import FakeQueue


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _block(q: FakeQueue):
    def block(conn, task_id, *, approval_id, reason=""):
        return q.transition(conn, task_id, TaskStatus.BLOCKED,
                            result={"blocked_on_approval": str(approval_id), "reason": reason})
    return block


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete,
                enqueue=q.enqueue, transition=q.transition, block=_block(q))


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(CodingTool())  # no sandbox — but a 🔴 PEND happens before execute
    return reg


# --------------------------------------------------------------------------- #
# routing: work.code / prototype → the coding path (not the work.* loop)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("task_type", ["work.code", "prototype"])
def test_coding_task_routes_to_code_path_and_pends_on_red_approval(task_type):
    """A "Need Prototype" task dispatches the coding worker via the 🔴 policy gate;
    with no grant it PENDs and the task is parked ``blocked`` (resumed later)."""
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    reg = _registry()
    q.enqueue(None, workstream="test", type=task_type,
              payload={"goal": "build a landing page", "workspace": "/scratch/ws"})

    # conn (first arg) is None → the real invoke's no-conn path pends ephemerally
    # (🔴, no grant), so the task parks blocked without any DB.
    r = run_once(None, "w1", sink, registry=reg, config=load_policy(), **_seams(q))
    assert r is not None
    assert r.kind == "code"          # coding path, NOT "work"
    assert r.outcome == "blocked"

    task = [t for t in q.tasks.values()][0]
    assert task.status is TaskStatus.BLOCKED  # parked on the approval

    # The dispatch went through the policy gate as a 🔴 code.run and never merged.
    assert "policy.decision" in sink.types()
    dec = [e for e in sink.events if e.type == "policy.decision"][0]
    assert dec.payload["tier"] == "red"
    assert "code.run" in dec.payload["required_capabilities"]


# --------------------------------------------------------------------------- #
# _handle_code branches (stubbed invoke — no DB, no sandbox)
# --------------------------------------------------------------------------- #

def _decision(effect, tier=ActionTier.RED):
    return Decision(effect=effect, tier=tier, reason="stub", role="builder", tool="coding")


def test_executed_success_merges():
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    task = q.enqueue(None, workstream="test", type="work.code",
                     payload={"goal": "g", "workspace": "/ws"})
    q.claim(None, worker_id="w1")  # → in_progress

    def fake_invoke(**kw):
        assert kw["role"] == "builder" and kw["tool_name"] == "coding"
        assert kw["goal"] == "g" and kw["workspace"] == "/ws"
        return InvokeResult(
            status=InvokeStatus.EXECUTED, decision=_decision(Effect.ALLOW), tool="coding",
            result=ToolResult(ok=True, output={"exit_code": 0, "produced_files": "/ws"},
                              metadata={"worker_cmd": "opencode"}),
        )

    r = _handle_code(None, q.tasks[task.id], sink, registry=_registry(), config=None,
                     heartbeat=q.heartbeat, complete=q.complete, block=_block(q),
                     worker_id="w1", invoke_tool=fake_invoke)
    assert r.kind == "code" and r.outcome == "done"
    assert q.tasks[task.id].status is TaskStatus.MERGED
    assert q.tasks[task.id].result["produced_files"] == "/ws"


def test_executed_worker_failure_abandons():
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    task = q.enqueue(None, workstream="test", type="work.code", payload={"goal": "g"})
    q.claim(None, worker_id="w1")

    def fake_invoke(**kw):
        return InvokeResult(
            status=InvokeStatus.EXECUTED, decision=_decision(Effect.ALLOW), tool="coding",
            result=ToolResult(ok=False, output={"exit_code": 2}, error="worker error"),
        )

    r = _handle_code(None, q.tasks[task.id], sink, registry=_registry(), config=None,
                     heartbeat=q.heartbeat, complete=q.complete, block=_block(q),
                     worker_id="w1", invoke_tool=fake_invoke)
    assert r.outcome == "failed"
    assert q.tasks[task.id].status is TaskStatus.ABANDONED


def test_denied_dispatch_abandons():
    sink = MemoryEventSink()
    q = FakeQueue(sink)
    task = q.enqueue(None, workstream="test", type="work.code", payload={"goal": "g"})
    q.claim(None, worker_id="w1")

    def fake_invoke(**kw):
        return InvokeResult(status=InvokeStatus.DENIED, decision=_decision(Effect.DENY),
                            tool="coding", result=None)

    r = _handle_code(None, q.tasks[task.id], sink, registry=_registry(), config=None,
                     heartbeat=q.heartbeat, complete=q.complete, block=_block(q),
                     worker_id="w1", invoke_tool=fake_invoke)
    assert r.outcome == "failed"
    assert q.tasks[task.id].status is TaskStatus.ABANDONED
