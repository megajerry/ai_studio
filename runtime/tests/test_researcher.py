"""Researcher role tests (ADR-0003 / ADR-0005 / ADR-0008).

Pure-logic tests (finding distillation, bounds, adaptive-lite) and worker-wiring
tests (research dispatch, NO research-loop) run with NO database via an in-memory
fake queue. The live-DB tests exercise ``run_research`` end-to-end:

- search runs through the policy-gated cached gateway (``net.fetch``); a role
  without that capability is DENIED (no provider call, no cache write);
- ≥1 lesson is distilled into Knowledge memory and is then ``recall_lessons``-able;
- ``research.completed`` carries counts / topic-hash / ids — never result bodies
  or the stored lesson text (invariants 5 & 6);
- an (optional) drafted candidate skill is ``reviewed: false`` and is therefore
  excluded by the skills inject gate (review-before-use);
- a research task enqueues nothing (no research-loop).

They SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.enforce import MemoryEventSink
from runtime.memory import recall_lessons
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus
from runtime.policy import PolicyConfig
from runtime.roles.researcher import (
    DEFAULT_RESULTS,
    EVENT_RESEARCH_COMPLETED,
    MAX_LESSONS,
    RESEARCH_TASK_TYPE,
    ResearchResult,
    distill_findings,
    run_research,
)
from runtime.search import (
    EVENT_SEARCH_CACHE_MISS,
    EVENT_SEARCH_DENIED,
    EVENT_SEARCH_PROVIDER_CALL,
    SearchDenied,
    SearchResult,
)
from runtime.skills import filter_injectable, parse_skill
from runtime.tools import FilesystemTool, ShellTool, ToolRegistry
from runtime.worker import run_once

# Policies used across the live tests — least privilege per scenario.
NET_ONLY = PolicyConfig(roles={"researcher": frozenset({Capability.NET_FETCH})})
NET_AND_WRITE = PolicyConfig(
    roles={"researcher": frozenset({Capability.NET_FETCH, Capability.FS_WRITE})}
)
READ_ONLY = PolicyConfig(roles={"researcher": frozenset({Capability.FS_READ})})


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("MODELS_DRY_RUN", "1")
    monkeypatch.setenv("SEARCH_DRY_RUN", "1")

    def boom(*a, **k):  # pragma: no cover - a network call would be a bug
        raise AssertionError("network call attempted in a test")

    monkeypatch.setattr(httpx, "post", boom)


def _results(n: int) -> list[SearchResult]:
    return [
        SearchResult(title=f"result {i}", url=f"https://example.invalid/{i}", score=1.0 - i / 10)
        for i in range(n)
    ]


# ===========================================================================
# Pure logic — finding distillation (no DB, no model, no network)
# ===========================================================================


def test_distill_never_empty_even_with_no_results():
    assert distill_findings("anything", [])


def test_distill_is_bounded():
    lessons = distill_findings("ai llm agent framework", _results(20), max_lessons=MAX_LESSONS)
    assert 1 <= len(lessons) <= MAX_LESSONS
    assert len(distill_findings("ai agent tooling", _results(20), max_lessons=1)) == 1


def test_distill_fast_moving_domain_adds_revisit_lesson():
    # A fast-moving domain (ADR-0003) earns an extra "revisit" lesson.
    fast = distill_findings("LLM agent security best-practice", _results(1))
    slow = distill_findings("medieval pottery glazing", _results(1))
    assert any("fast-moving" in l for l in fast)
    assert not any("fast-moving" in l for l in slow)


def test_distill_references_top_source():
    lessons = distill_findings("topic x", _results(3))
    assert any("result 0" in l for l in lessons)


# ===========================================================================
# Worker wiring — research dispatch + NO research-loop (fake queue, no DB)
# ===========================================================================


class FakeQueue:
    """Minimal in-memory queue mirroring the real enqueue/claim/heartbeat/complete."""

    def __init__(self) -> None:
        self.tasks: dict = {}
        self.order: list = []

    def enqueue(self, conn, *, workstream, type, payload=None, priority=0,
                assignee=None, budget_tokens=None) -> Task:
        now = datetime.now(timezone.utc)
        t = Task(id=uuid4(), workstream=workstream, type=type, status=TaskStatus.UP_FOR_GRABS,
                 priority=priority, payload=payload or {}, created_at=now, updated_at=now)
        self.tasks[t.id] = t
        self.order.append(t.id)
        return t

    def claim(self, conn, *, worker_id, assignee=None, workstream=None):
        ids = [i for i in self.order if self.tasks[i].status is TaskStatus.UP_FOR_GRABS
               and (workstream is None or self.tasks[i].workstream == workstream)]
        if not ids:
            return None
        t = self.tasks[ids[0]].model_copy(update={
            "status": TaskStatus.IN_PROGRESS, "claimed_by": worker_id,
            "heartbeat_at": datetime.now(timezone.utc)})
        self.tasks[t.id] = t
        return t

    def heartbeat(self, conn, task_id, worker_id):
        return self.tasks.get(task_id)

    def complete(self, conn, task_id, *, result=None, status=TaskStatus.MERGED,
                 spent_tokens=None, force=False):
        t = self.tasks.get(task_id)
        if t is None:
            return None
        t = t.model_copy(update={"status": status, "result": result})
        self.tasks[task_id] = t
        return t


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _seams(q: FakeQueue) -> dict:
    return dict(claim=q.claim, heartbeat=q.heartbeat, complete=q.complete, enqueue=q.enqueue)


def test_research_task_dispatches_to_researcher_and_enqueues_nothing(tmp_path):
    """A research task is handled by run_research and NEVER enqueues (no loop)."""
    sink = MemoryEventSink()
    q = FakeQueue()
    q.enqueue(None, workstream="t", type=RESEARCH_TASK_TYPE, payload={"topic": "x"})
    before = len(q.tasks)

    def fake_run_research(conn, task, s, **kw):
        # A research task must never receive an `enqueue` seam from the worker.
        assert "enqueue" not in kw
        return ResearchResult(topic_hash="h", results_count=5, lessons_count=2)

    r = run_once(None, "w1", sink, registry=_registry(tmp_path),
                 run_research=fake_run_research, **_seams(q))
    assert r is not None and r.kind == "research" and r.outcome == "done"
    # No new task was enqueued by the research handler.
    assert len(q.tasks) == before


# ===========================================================================
# Live DB — search gateway path, distilled lessons, no leak, no loop
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


def _research_task(ws: str, **payload) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type=RESEARCH_TASK_TYPE,
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload=payload)


@pytestmark_db
def test_research_uses_gateway_and_distills_recallable_lesson(conn):
    ws = f"res-{uuid4().hex[:12]}"
    # A unique topic guarantees a cold cache → a real provider call this run.
    topic = f"best practices for LLM agent tool use {ws}"
    sink = MemoryEventSink()
    result = run_research(conn, _research_task(ws, topic=topic), sink, policy=NET_ONLY)
    assert isinstance(result, ResearchResult)
    assert result.results_count == DEFAULT_RESULTS
    assert result.lessons_count >= 1
    # The search went through the policy-gated gateway (miss → provider), NOT direct.
    assert EVENT_SEARCH_CACHE_MISS in sink.types()
    assert EVENT_SEARCH_PROVIDER_CALL in sink.types()
    # The distilled lesson(s) are now recallable from Knowledge memory.
    lessons = recall_lessons(conn, ws, "best practices LLM agent tool", k=5)
    assert len(lessons) >= 1


@pytestmark_db
def test_search_denied_for_role_without_net_fetch(conn):
    """A role lacking net.fetch is denied by the gateway — nothing fetched."""
    ws = f"res-{uuid4().hex[:12]}"
    sink = MemoryEventSink()
    with pytest.raises(SearchDenied):
        run_research(conn, _research_task(ws, topic="anything"), sink, policy=READ_ONLY)
    assert EVENT_SEARCH_DENIED in sink.types()
    # Denied before the provider ran: no provider call, and no lesson stored.
    assert EVENT_SEARCH_PROVIDER_CALL not in sink.types()
    assert recall_lessons(conn, ws, "anything", k=5, include_global=False) == []


@pytestmark_db
def test_research_completed_event_carries_no_bodies(conn):
    ws = f"res-{uuid4().hex[:12]}"
    sink = MemoryEventSink()
    run_research(conn, _research_task(ws, topic="secret-topic vulnerability xyz"),
                 sink, policy=NET_ONLY)

    completed = [e for e in sink.events if e.type == EVENT_RESEARCH_COMPLETED]
    assert len(completed) == 1
    payload = completed[0].payload
    # Exactly the safe fields — counts / topic-hash / a bool. No raw topic, no body.
    assert set(payload) == {"topic_hash", "results_count", "lessons_count", "skill_drafted"}
    assert payload["lessons_count"] >= 1
    # The raw topic never appears in any emitted event payload.
    assert all("secret-topic" not in str(e.payload) for e in sink.events)
    # The stored lesson text never appears in the research.completed payload.
    lessons = recall_lessons(conn, ws, "vulnerability", k=5)
    assert lessons
    for item in lessons:
        assert item.text not in str(payload)


@pytestmark_db
def test_candidate_skill_draft_is_unreviewed_and_excluded_by_inject_gate(conn, tmp_path):
    ws = f"res-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    result = run_research(
        conn, _research_task(ws, topic="prompt injection defenses"),
        sink, tool_registry=reg, policy=NET_AND_WRITE, draft_skill=True,
    )
    assert result.skill_draft_status == "executed"
    assert result.skill_path is not None
    assert result.skill_reviewed is False

    skill_file = tmp_path / result.skill_path
    assert skill_file.exists()
    skill = parse_skill(skill_file.read_text(), source_path=skill_file)
    # Drafted skills are unreviewed → excluded by the inject gate (review-before-use).
    assert skill.reviewed is False
    assert "researcher" in skill.source
    injectable, skipped = filter_injectable([skill])
    assert injectable == []
    assert skill in skipped


@pytestmark_db
def test_candidate_skill_draft_denied_without_fs_write(conn, tmp_path):
    """Drafting is policy-gated: a researcher without fs.write cannot write a skill."""
    ws = f"res-{uuid4().hex[:12]}"
    reg = _registry(tmp_path)
    sink = MemoryEventSink()
    result = run_research(
        conn, _research_task(ws, topic="anything"),
        sink, tool_registry=reg, policy=NET_ONLY, draft_skill=True,
    )
    assert result.skill_draft_status == "denied"
    assert result.skill_path is None
    # Nothing was written to the tool root.
    assert not any(tmp_path.rglob("SKILL.md"))
    # But the lessons path still completed fully.
    assert result.lessons_count >= 1


@pytestmark_db
def test_run_research_enqueues_nothing_live(conn):
    """A live research task stores lessons but never touches the task queue."""
    ws = f"res-{uuid4().hex[:12]}"
    sink = MemoryEventSink()
    run_research(conn, _research_task(ws, topic="observability"), sink, policy=NET_ONLY)
    assert "task.created" not in sink.types()
