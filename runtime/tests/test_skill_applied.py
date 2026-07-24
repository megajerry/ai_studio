"""P0 attribution: the body-free ``skill.applied`` event (ADR-0024).

Pure Python (no DB): a reviewed skill injected into a role's prompt emits ONE
``skill.applied`` event naming ONLY the injected skill(s) + the role (no skill
body); an unreviewed/skipped skill is never injected, so it emits nothing. Covers
the helper directly and through a role (the PM), proving the wire is live.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from runtime.enforce import MemoryEventSink
from runtime.event_types import EVENT_SKILL_APPLIED
from runtime.models import Task, TaskStatus
from runtime.skills import Skill, SkillRegistry, emit_skill_applied


def _skill(name: str, *, reviewed: bool) -> Skill:
    return Skill(name=name, description=f"{name} desc", instructions=f"BODY OF {name}",
                 triggers=["pm", "plan"], reviewed=reviewed, source="in-repo")


# --- the helper -------------------------------------------------------------


def test_emit_names_only_reviewed_and_is_body_free():
    sink = MemoryEventSink()
    tid = uuid4()
    names = emit_skill_applied(
        sink, task_id=tid, role="pm", workstream="ws",
        skills=[_skill("define-success-criteria", reviewed=True),
                _skill("sketchy-import", reviewed=False)],
    )
    assert names == ["define-success-criteria"]  # unreviewed excluded
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.type == EVENT_SKILL_APPLIED
    assert ev.task_id == tid and ev.workstream == "ws"
    # BODY-FREE: only the skill NAMES + role — never any instruction body.
    assert ev.payload == {"skills": ["define-success-criteria"], "role": "pm"}
    assert "BODY OF" not in str(ev.payload)


def test_no_event_when_only_unreviewed_or_empty():
    sink = MemoryEventSink()
    # Only an unreviewed skill → nothing is injected → nothing to attribute.
    assert emit_skill_applied(sink, task_id=uuid4(), role="pm", workstream="ws",
                              skills=[_skill("sketchy-import", reviewed=False)]) == []
    # No skills at all (no registry) → no-op.
    assert emit_skill_applied(sink, task_id=uuid4(), role="pm", workstream="ws",
                              skills=None) == []
    assert emit_skill_applied(sink, task_id=uuid4(), role="pm", workstream="ws",
                              skills=[]) == []
    assert sink.events == []


def test_allow_unreviewed_attributes_what_was_actually_injected():
    """When a role injects with allow_unreviewed=True the unreviewed skill IS in the
    prompt, so attribution must name it too (attribute what was injected)."""
    sink = MemoryEventSink()
    names = emit_skill_applied(
        sink, task_id=uuid4(), role="pm", workstream="ws",
        skills=[_skill("reviewed-one", reviewed=True),
                _skill("unreviewed-one", reviewed=False)],
        allow_unreviewed=True,
    )
    assert set(names) == {"reviewed-one", "unreviewed-one"}


# --- through a role (PM) ----------------------------------------------------


def _pm_task(goal: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream="prod", type="pm.tick", status=TaskStatus.IN_PROGRESS,
                priority=0, payload={"goal": goal}, created_at=now, updated_at=now)


def _run_pm(sink, reg):
    import json as _json
    from runtime.roles import pm as pm_mod

    def fake_call_model(*, messages, **kw):
        plan = {"restated_goal": "g", "confidence": 0.9, "feasible": True,
                "work_items": [{"title": "t", "instructions": "i",
                                "success_criterion": "c", "marker": "m1"}]}
        return type("_C", (), {"text": _json.dumps(plan)})()

    pm_mod.run_pm_tick(
        None, _pm_task("Ship the release"), sink, registry=None, skills=reg,
        enqueue=lambda conn, **kw: _pm_task(kw.get("payload", {}).get("goal", "")),
        call_model=fake_call_model,
    )


def test_pm_role_emits_skill_applied_for_injected_reviewed_skill():
    sink = MemoryEventSink()
    reg = SkillRegistry([_skill("define-success-criteria", reviewed=True),
                         _skill("sketchy-import", reviewed=False)])
    _run_pm(sink, reg)
    applied = [e for e in sink.events if e.type == EVENT_SKILL_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload == {"skills": ["define-success-criteria"], "role": "pm"}


def test_pm_role_emits_nothing_when_only_unreviewed_selected():
    sink = MemoryEventSink()
    reg = SkillRegistry([_skill("sketchy-import", reviewed=False)])
    _run_pm(sink, reg)
    assert [e for e in sink.events if e.type == EVENT_SKILL_APPLIED] == []
