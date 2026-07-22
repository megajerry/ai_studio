"""Role unit tests — PM / Executor / Verifier in isolation, keyless & DB-free.

Each role is driven with a MemoryEventSink and (where a tool is needed) a real
FilesystemTool confined to a pytest tmp dir. No network, no database, no keys:
``call_model`` falls back to the dry-run provider. Also asserts the policy gate
refuses 🔴 tool calls (delete / shell) so a role can never escalate privilege.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime.enforce import InvokeStatus, MemoryEventSink, invoke
from runtime.models import Task, TaskStatus
from runtime.policy import Effect, load_policy
from runtime.roles.executor import ExecutorResult, run_executor
from runtime.roles.pm import WORK_TASK_TYPE, run_pm_tick
from runtime.roles.verifier import verify
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover - only fires on a real network call
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _task(type_: str, payload: dict | None = None, workstream: str = "test") -> Task:
    now = datetime.now(timezone.utc)
    return Task(
        id=uuid4(),
        workstream=workstream,
        type=type_,
        status=TaskStatus.IN_PROGRESS,
        priority=0,
        payload=payload or {},
        created_at=now,
        updated_at=now,
    )


def _fs_registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


# --- PM ---------------------------------------------------------------------


def test_pm_plans_and_enqueues_exactly_one_work_task():
    sink = MemoryEventSink()
    enqueued = []

    def fake_enqueue(conn, *, workstream, type, payload=None, priority=0, **kw) -> Task:
        t = _task(type, payload, workstream)
        t = t.model_copy(update={"status": TaskStatus.QUEUED})
        enqueued.append(t)
        return t

    task = _task("pm.tick", {"goal": "Ship the thing"})
    plan = run_pm_tick(None, task, sink, enqueue=fake_enqueue)

    assert plan.goal == "Ship the thing"
    assert plan.marker and plan.criterion
    # Exactly ONE work task enqueued, carrying the goal + criterion + marker.
    assert len(enqueued) == 1
    work = enqueued[0]
    assert work.type == WORK_TASK_TYPE
    assert work.payload["marker"] == plan.marker
    assert work.payload["criterion"] == plan.criterion
    # The confidence-gate model call and the plan event were both emitted.
    types = sink.types()
    assert "model.routed" in types and "model.call" in types
    assert "pm.planned" in types


def test_pm_uses_default_objective_when_no_goal():
    sink = MemoryEventSink()
    enqueued = []
    plan = run_pm_tick(
        None,
        _task("pm.tick", {"kind": "pulse"}),
        sink,
        enqueue=lambda conn, **kw: enqueued.append(kw) or _task(kw["type"], kw.get("payload")),
    )
    assert plan.goal  # a non-empty default objective
    assert len(enqueued) == 1


# --- Executor ---------------------------------------------------------------


def test_executor_writes_artifact_via_invoke_and_calls_model(tmp_path):
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    marker = "studio-ok:abc"
    task = _task(WORK_TASK_TYPE, {"goal": "g", "criterion": "c", "marker": marker})

    res = run_executor(None, task, sink, registry=reg, config=load_policy())

    assert res.ok and res.invoke_status == InvokeStatus.EXECUTED.value
    assert res.artifact_path
    written = (tmp_path / res.artifact_path).read_text()
    assert marker in written
    types = sink.types()
    # Model call went through call_model; tool call went through the policy gate.
    assert "model.routed" in types and "model.call" in types
    assert "policy.decision" in types and "tool.invoked" in types
    assert "executor.acted" in types


# --- Verifier ---------------------------------------------------------------


def test_verify_passes_when_marker_present(tmp_path):
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    marker = "studio-ok:xyz"
    (tmp_path / "art.txt").write_text(f"{marker}\nblah\n")
    task = _task(WORK_TASK_TYPE, {"criterion": "c", "marker": marker})
    result = ExecutorResult(ok=True, artifact_path="art.txt", marker=marker,
                            invoke_status="executed")

    verdict = verify(None, task, result, sink, registry=reg, config=load_policy())

    assert verdict.passed
    types = sink.types()
    assert "model.routed" in types and "model.call" in types
    assert "policy.decision" in types and "tool.invoked" in types  # the read
    assert "verify.passed" in types


def test_verify_fails_when_marker_missing(tmp_path):
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    (tmp_path / "art.txt").write_text("nothing useful here\n")
    task = _task(WORK_TASK_TYPE, {"marker": "studio-ok:xyz"})
    result = ExecutorResult(ok=True, artifact_path="art.txt", marker="studio-ok:xyz",
                            invoke_status="executed")

    verdict = verify(None, task, result, sink, registry=reg, config=load_policy())
    assert not verdict.passed
    assert "verify.failed" in sink.types()


def test_verify_fails_when_no_artifact(tmp_path):
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    task = _task(WORK_TASK_TYPE, {"marker": "studio-ok:xyz"})
    result = ExecutorResult(ok=False, artifact_path=None, marker="studio-ok:xyz",
                            invoke_status="denied")
    verdict = verify(None, task, result, sink, registry=reg, config=load_policy())
    assert not verdict.passed and "no artifact" in verdict.reason


# --- Policy gate: 🔴 tools never execute for a role that lacks the capability --


def test_executor_role_cannot_delete(tmp_path):
    """fs.delete is 🔴 and the executor role is not granted it → DENY, no exec."""
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    (tmp_path / "victim.txt").write_text("data")
    res = invoke("executor", "filesystem", registry=reg, config=load_policy(),
                 events=sink, op="delete", path="victim.txt")
    assert res.status is InvokeStatus.DENIED
    assert res.result is None
    assert res.decision.effect is Effect.DENY
    assert (tmp_path / "victim.txt").exists()  # nothing was deleted


def test_executor_role_cannot_shell(tmp_path):
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    res = invoke("executor", "shell", registry=reg, config=load_policy(),
                 events=sink, command="echo hi")
    assert res.status is InvokeStatus.DENIED and res.result is None
    assert "tool.invoked" not in sink.types()


def test_red_capability_role_needs_approval_not_execution(tmp_path):
    """A role WITH a 🔴 capability (deployer→shell.exec) escalates to
    NEEDS_APPROVAL and still does not execute."""
    sink = MemoryEventSink()
    reg = _fs_registry(tmp_path)
    res = invoke("deployer", "shell", registry=reg, config=load_policy(),
                 events=sink, command="deploy prod")
    assert res.status is InvokeStatus.PENDING and res.result is None
    assert res.decision.effect is Effect.NEEDS_APPROVAL
    assert "approval.requested" in sink.types()
    assert "tool.invoked" not in sink.types()
