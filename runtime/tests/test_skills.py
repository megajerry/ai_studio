"""Skills layer tests — parse / registry select / inject / role composition.

Pure Python: no database, no network, no keys. Covers the Agent Skills standard
(ADR-0008) invariants — on-demand relevant selection, the review-before-use gate,
graceful failure on malformed skills, and a role composing its prompt with the
selected + reviewed skill only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from runtime.models import Task, TaskStatus
from runtime.skills import (
    Skill,
    SkillError,
    SkillRegistry,
    compose,
    compose_prompt,
    default_root,
    load_skill,
    parse_skill,
)

VALID_SKILL = """---
name: define-success-criteria
description: Fix one checkable success criterion before executing.
triggers: [pm, plan, success criteria]
when_to_use: When the PM plans a task.
reviewed: true
source: in-repo
---

# Define success criteria

Restate the goal and define ONE independently checkable criterion.
"""


# --- parsing ----------------------------------------------------------------


def test_parse_valid_skill():
    skill = parse_skill(VALID_SKILL, source_path="skills/x/SKILL.md")
    assert skill.name == "define-success-criteria"
    assert skill.reviewed is True
    assert skill.source == "in-repo"
    assert "pm" in skill.triggers and "plan" in skill.triggers
    assert skill.when_to_use.startswith("When the PM")
    # The markdown body is captured as instructions (frontmatter stripped).
    assert "Restate the goal" in skill.instructions
    assert "name: define-success-criteria" not in skill.instructions
    assert skill.path and skill.path.endswith("SKILL.md")


def test_triggers_accepts_comma_string():
    text = VALID_SKILL.replace("triggers: [pm, plan, success criteria]", "triggers: pm, plan")
    skill = parse_skill(text)
    assert skill.triggers == ["pm", "plan"]


@pytest.mark.parametrize(
    "bad, needle",
    [
        # No frontmatter fence at all.
        ("just some markdown, no frontmatter\n", "missing opening"),
        # Opening fence but never closed.
        ("---\nname: x\ndescription: y\nno close\n", "unterminated"),
        # Broken YAML inside the fence.
        ("---\nname: [unclosed\n---\nbody\n", "invalid YAML"),
        # Frontmatter is a list, not a mapping.
        ("---\n- a\n- b\n---\nbody\n", "must be a YAML mapping"),
        # Missing required field `description`.
        ("---\nname: x\n---\nbody\n", "invalid skill metadata"),
        # Missing required field `name`.
        ("---\ndescription: y\n---\nbody\n", "invalid skill metadata"),
        # Empty name.
        ("---\nname: '  '\ndescription: y\n---\nbody\n", "invalid skill metadata"),
    ],
)
def test_malformed_frontmatter_raises_clear_error_no_crash(bad, needle):
    with pytest.raises(SkillError) as exc:
        parse_skill(bad, source_path="skills/bad/SKILL.md")
    msg = str(exc.value)
    assert needle in msg
    # The message is path-qualified for debuggability.
    assert "skills/bad/SKILL.md" in msg


# --- registry: discovery + selection ----------------------------------------


def _reg() -> SkillRegistry:
    return SkillRegistry(
        [
            Skill(name="define-success-criteria", description="pm planning gate",
                  triggers=["pm", "plan", "success criteria"], reviewed=True, source="in-repo"),
            Skill(name="retrospective", description="distill lessons after a task",
                  triggers=["retro", "lesson", "post-mortem"], reviewed=True, source="in-repo"),
            Skill(name="code-review", description="independent review of a change",
                  triggers=["review", "reviewer", "safety"], reviewed=True, source="in-repo"),
            Skill(name="sketchy-import", description="unreviewed external skill",
                  triggers=["pm", "plan"], reviewed=False, source="some-github-repo"),
        ]
    )


def test_select_returns_only_relevant():
    reg = _reg()
    picked = {s.name for s in reg.select("pm plan the release")}
    # Relevant to planning; NOT the retro/review skills.
    assert "define-success-criteria" in picked
    assert "retrospective" not in picked
    assert "code-review" not in picked


def test_select_respects_limit():
    reg = _reg()
    # "pm plan" matches both define-success-criteria and the (unreviewed) import.
    assert len(reg.select("pm plan", limit=1)) == 1
    assert reg.select("pm plan", limit=0) == []


def test_select_can_drop_unreviewed():
    reg = _reg()
    all_rel = {s.name for s in reg.select("pm plan")}
    assert "sketchy-import" in all_rel  # selection alone does not gate on review
    reviewed_only = {s.name for s in reg.select("pm plan", include_unreviewed=False)}
    assert "sketchy-import" not in reviewed_only


def test_discover_skips_malformed(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "SKILL.md").write_text(VALID_SKILL)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text("no frontmatter here\n")
    reg = SkillRegistry.discover(tmp_path)
    assert reg.names() == ["define-success-criteria"]  # bad one skipped, no crash


def test_discover_missing_root_is_empty(tmp_path):
    reg = SkillRegistry.discover(tmp_path / "does-not-exist")
    assert len(reg) == 0


# --- injection: review gate + relevance -------------------------------------


def test_inject_includes_reviewed_relevant_excludes_unreviewed():
    reg = _reg()
    selected = reg.select("pm plan")  # includes reviewed + the unreviewed import
    result = compose("BASE PROMPT", selected)
    # Reviewed, relevant skill is present in the prompt.
    assert "define-success-criteria" in result.prompt
    assert "BASE PROMPT" in result.prompt
    # Unreviewed skill is excluded by default and reported as skipped.
    assert "sketchy-import" not in result.prompt
    assert {s.name for s in result.skipped_unreviewed} == {"sketchy-import"}
    assert {s.name for s in result.included} == {"define-success-criteria"}


def test_inject_excludes_irrelevant():
    reg = _reg()
    selected = reg.select("pm plan")
    prompt = compose_prompt("BASE", selected)
    # retrospective/code-review were never selected → never in the prompt.
    assert "retrospective" not in prompt
    assert "code-review" not in prompt


def test_inject_allow_unreviewed_includes_but_still_only_selected():
    reg = _reg()
    selected = reg.select("pm plan")
    prompt = compose_prompt("BASE", selected, allow_unreviewed=True)
    assert "sketchy-import" in prompt


def test_inject_empty_when_no_reviewed_skills():
    unreviewed = [Skill(name="x", description="d", reviewed=False)]
    result = compose("BASE", unreviewed)
    assert result.prompt == "BASE"  # nothing injected, base returned unchanged
    assert result.included == []


# --- the shipped example skills ---------------------------------------------


def test_example_skills_all_load_and_are_reviewed():
    reg = SkillRegistry.discover(default_root())
    names = set(reg.names())
    assert {"define-success-criteria", "retrospective", "code-review"} <= names
    for skill in reg.all():
        assert skill.reviewed is True, f"{skill.name} must be reviewed"
        assert skill.source == "in-repo"
        assert skill.instructions.strip()  # non-empty body
        assert skill.triggers  # discoverable


def test_rigorous_review_skill_loads_and_is_reviewed():
    """The evidence-over-claims doctrine (ADR-0014) ships as a reviewed skill that
    a validator selects on its validation triggers."""
    reg = SkillRegistry.discover(default_root())
    skill = reg.get("rigorous-review")
    assert skill is not None, "rigorous-review skill must be discoverable"
    assert skill.reviewed is True and skill.source == "in-repo"
    # Selectable by every validation trigger the doctrine claims.
    for query in ("verify", "review", "validate", "audit", "check"):
        assert skill in reg.select(query), f"not selected for {query!r}"
    # The body encodes the doctrine, not just a title.
    body = skill.instructions.lower()
    assert "evidence" in body and "unverified" in body
    assert "commit message" in body  # explicitly names what is NOT evidence


# --- role prompt composition (PM) -------------------------------------------


def _pm_task(goal: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream="test", type="pm.tick", status=TaskStatus.IN_PROGRESS,
                priority=0, payload={"goal": goal}, created_at=now, updated_at=now)


def test_pm_composes_prompt_with_selected_reviewed_skill(monkeypatch):
    """The PM's confidence-gate prompt includes the selected reviewed skill and
    excludes irrelevant/unreviewed ones — proving the role uses the skills layer."""
    from runtime.roles import pm as pm_mod

    captured: dict = {}

    def fake_call_model(*, messages, **kw):
        captured["prompt"] = messages[0]["content"]

        class _C:
            text = "ok"

        return _C()

    monkeypatch.setattr(pm_mod, "call_model", fake_call_model)

    reg = SkillRegistry(
        [
            Skill(name="define-success-criteria", description="pm planning gate",
                  triggers=["pm", "plan", "success criteria"], reviewed=True, source="in-repo",
                  instructions="Restate the goal and define ONE criterion."),
            Skill(name="retrospective", description="distill lessons",
                  triggers=["retro", "lesson"], reviewed=True, source="in-repo",
                  instructions="Distill lessons."),
            Skill(name="sketchy-import", description="unreviewed pm plan skill",
                  triggers=["pm", "plan"], reviewed=False, source="ext",
                  instructions="do sketchy things"),
        ]
    )

    enqueued: list = []
    pm_mod.run_pm_tick(
        None,
        _pm_task("Ship the release"),
        registry=None,
        skills=reg,
        enqueue=lambda conn, **kw: enqueued.append(kw)
        or _pm_task(kw.get("payload", {}).get("goal", "")),
    )

    prompt = captured["prompt"]
    assert "You are the studio PM" in prompt          # base persona preserved
    assert "define-success-criteria" in prompt         # selected + reviewed → injected
    assert "Restate the goal and define ONE" in prompt
    assert "retrospective" not in prompt               # irrelevant → not selected
    assert "sketchy-import" not in prompt              # unreviewed → gated out
    assert len(enqueued) == 1                          # behavior preserved


def test_pm_prompt_unchanged_without_skills(monkeypatch):
    """No registry → the inline base prompt only (behavior-preserving)."""
    from runtime.roles import pm as pm_mod

    captured: dict = {}
    monkeypatch.setattr(
        pm_mod, "call_model",
        lambda *, messages, **kw: captured.setdefault("p", messages[0]["content"])
        or type("C", (), {"text": "ok"})(),
    )
    pm_mod.run_pm_tick(None, _pm_task("g"), enqueue=lambda conn, **kw: _pm_task("g"))
    assert "### Skills" not in captured["p"]
