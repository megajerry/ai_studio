"""Vertical-customization seam tests — prompt assembler + verify-checker registry.

Two seams that let a vertical inject its own framing/objective/prompt and domain
checks while reusing the shared role procedures + learning/retro/telemetry:

1. :func:`runtime.roles.prompt.compose_role_prompt` — the single role prompt
   assembler (shared base → charter → overlay → skills → lessons → task). Proven
   *behavior-preserving* (identical to the old inline composition with no
   charter/overlay/task) and *layering* correctly + bounded when layers are given.
2. :mod:`runtime.roles.checkers` — the pluggable verify-checker registry: the
   default ``marker`` checker keeps back-compat, a vertical registers its own
   domain checker (``video_audit`` here) dispatched by a STRUCTURED criterion, the
   verdict rests on FACTS (a false "done" claim still fails), and an unknown check
   name errors clearly.

Keyless + DB-free: ``call_model`` falls back to the dry-run provider, tools are a
real FilesystemTool confined to a pytest tmp dir.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from runtime.enforce import InvokeStatus, MemoryEventSink
from runtime.models import Task, TaskStatus
from runtime.policy import load_policy
from runtime.roles import verifier as verifier_mod
from runtime.roles.checkers import (
    ArtifactRef,
    CheckerRegistry,
    CheckResult,
    UnknownChecker,
    default_registry,
    marker_check,
    resolve_criterion,
)
from runtime.roles.executor import ExecutorResult, run_executor
from runtime.roles.pm import run_pm_tick
from runtime.roles.prompt import compose_role_prompt
from runtime.roles.verifier import verify
from runtime.skills import Skill, compose_prompt
from runtime.roles.lessons import compose_lessons
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
        id=uuid4(), workstream=workstream, type=type_, status=TaskStatus.IN_PROGRESS,
        priority=0, payload=payload or {}, created_at=now, updated_at=now,
    )


def _fs_registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    reg.register(ShellTool())
    return reg


def _skill(name: str, instructions: str, *, reviewed: bool = True) -> Skill:
    return Skill(name=name, description=f"{name} desc", instructions=instructions,
                 triggers=[name], reviewed=reviewed, source="in-repo")


# ===========================================================================
# 1. Prompt assembler — behavior-preserving + layering
# ===========================================================================


def test_assembler_behavior_preserving_base_only():
    """No layers → the exact base prompt, byte-for-byte (behavior-preserving)."""
    base = "You are the studio Executor. Goal: g. Success criterion: c."
    assert compose_role_prompt(base) == base


def test_assembler_matches_old_inline_skills_then_lessons_composition():
    """With only skills + lessons (the pre-seam inputs) the assembler output is
    IDENTICAL to the old inline `compose_lessons(compose_prompt(base, skills), …)`
    — so refactoring the roles onto it changes nothing."""
    base = "You are the studio PM. Goal: ship it."
    skills = [_skill("define-success-criteria", "State one checkable criterion.")]
    lessons = ["write the marker up front", "keep retries bounded"]

    old = compose_lessons(compose_prompt(base, skills), lessons)
    new = compose_role_prompt(base, skills=skills, lessons=lessons)
    assert new == old


def test_assembler_layers_appear_in_order_and_bounded():
    """All layers present → each shows up in a bounded, delimited section, in the
    fixed order base → charter → overlay → skills → lessons → task."""
    base = "BASE-PERSONA"
    out = compose_role_prompt(
        base,
        workstream_charter="CHARTER-video-channel",
        role_overlay="OVERLAY-editor-role",
        skills=[_skill("s1", "SKILL-BODY")],
        lessons=["LESSON-one"],
        task="TASK-render-the-clip",
    )
    # Every layer is present...
    for token in ("BASE-PERSONA", "CHARTER-video-channel", "OVERLAY-editor-role",
                  "SKILL-BODY", "LESSON-one", "TASK-render-the-clip"):
        assert token in out
    # ...bounded in delimited sections...
    assert "### Workstream charter" in out
    assert "### Role overlay" in out
    assert "### Skills" in out
    assert "### Lessons" in out
    assert "### Task" in out
    # ...and in the documented layering order.
    order = [out.index(t) for t in (
        "BASE-PERSONA", "CHARTER-video-channel", "OVERLAY-editor-role",
        "SKILL-BODY", "LESSON-one", "TASK-render-the-clip")]
    assert order == sorted(order)


def test_assembler_charter_and_overlay_optional_and_independent():
    base = "BASE"
    only_charter = compose_role_prompt(base, workstream_charter="CH")
    assert "### Workstream charter" in only_charter and "### Role overlay" not in only_charter
    only_overlay = compose_role_prompt(base, role_overlay="OV")
    assert "### Role overlay" in only_overlay and "### Workstream charter" not in only_overlay
    # Blank strings are treated as absent (no dangling header).
    assert compose_role_prompt(base, workstream_charter="  ", role_overlay="") == base


def test_assembler_respects_skill_review_gate():
    """Unreviewed skills are NOT injected by default (the ADR-0008 gate is reused)."""
    base = "BASE"
    out = compose_role_prompt(base, skills=[_skill("bad", "UNREVIEWED-BODY", reviewed=False)])
    assert "UNREVIEWED-BODY" not in out
    assert out == base  # nothing injected → base unchanged


# --- Roles compose via the assembler: charter/overlay flow through -----------


def test_pm_prompt_includes_charter_and_overlay(monkeypatch):
    captured: dict = {}

    def fake_call_model(**kw):
        captured["prompt"] = kw["messages"][0]["content"]
        return type("C", (), {"text": '{"confidence":0.9,"feasible":true,"work_items":['
                              '{"title":"t","success_criterion":"c","marker":"m"}]}'})()

    run_pm_tick(
        None, _task("pm.tick", {"goal": "Ship the thing"}), MemoryEventSink(),
        charter="VERTICAL-CHARTER", overlay="PM-OVERLAY",
        enqueue=lambda conn, **kw: _task("work.task", kw.get("payload")),
        call_model=fake_call_model,
    )
    assert "You are the studio PM" in captured["prompt"]  # shared base preserved
    assert "VERTICAL-CHARTER" in captured["prompt"]
    assert "PM-OVERLAY" in captured["prompt"]


def test_executor_prompt_includes_charter_and_overlay(tmp_path, monkeypatch):
    captured: dict = {}
    import runtime.roles.executor as executor_mod

    def fake_call_model(**kw):
        captured["prompt"] = kw["messages"][0]["content"]
        return type("C", (), {"text": "did it"})()

    monkeypatch.setattr(executor_mod, "call_model", fake_call_model)
    task = _task("work.task", {"goal": "g", "criterion": "c", "marker": "studio-ok:x"})
    run_executor(None, task, MemoryEventSink(), registry=_fs_registry(tmp_path),
                 config=load_policy(), charter="EXEC-CHARTER", overlay="EXEC-OVERLAY")
    assert "You are the studio Executor" in captured["prompt"]
    assert "EXEC-CHARTER" in captured["prompt"] and "EXEC-OVERLAY" in captured["prompt"]


def test_verifier_prompt_includes_charter_and_overlay(tmp_path, monkeypatch):
    captured: dict = {}
    (tmp_path / "art.txt").write_text("studio-ok:x\n")

    monkeypatch.setattr(
        verifier_mod, "call_model",
        lambda **kw: captured.setdefault("p", kw["messages"][0]["content"])
        or type("C", (), {"text": "ok"})(),
    )
    task = _task("work.task", {"criterion": "c", "marker": "studio-ok:x"})
    result = ExecutorResult(ok=True, artifact_path="art.txt", marker="studio-ok:x",
                            invoke_status="executed")
    verify(None, task, result, MemoryEventSink(), registry=_fs_registry(tmp_path),
           config=load_policy(), charter="VERIFY-CHARTER", overlay="VERIFY-OVERLAY")
    assert "You are the studio Verifier" in captured["p"]
    assert "VERIFY-CHARTER" in captured["p"] and "VERIFY-OVERLAY" in captured["p"]


# ===========================================================================
# 2. Pluggable verify-checker registry
# ===========================================================================


def test_resolve_criterion_backcompat_bare_marker():
    """No structured criterion → the marker checker on the fallback marker."""
    name, require = resolve_criterion({"marker": "studio-ok:z"}, fallback_marker="studio-ok:z")
    assert name == "marker" and require == "studio-ok:z"


def test_resolve_criterion_structured():
    payload = {"check": {"check": "video_audit", "require": {"min_seconds": 30}}}
    name, require = resolve_criterion(payload, fallback_marker="m")
    assert name == "video_audit" and require == {"min_seconds": 30}


def test_resolve_criterion_bare_check_name_string():
    name, require = resolve_criterion({"check": "marker", "marker": "m"}, fallback_marker="m")
    assert name == "marker" and require == "m"


def test_default_registry_marker_checker_pass_and_fail(tmp_path):
    reg = default_registry()
    assert reg.names() == ["marker"]
    (tmp_path / "ok.txt").write_text("studio-ok:z\nblah\n")
    task = _task("work.task", {"marker": "studio-ok:z"})
    ref = ArtifactRef(registry=_fs_registry(tmp_path), path="ok.txt", config=load_policy())

    good = reg.run("marker", None, task, ref, "studio-ok:z")
    assert good.passed and good.facts["marker"] == "studio-ok:z"

    (tmp_path / "bad.txt").write_text("nothing here\n")
    ref_bad = ArtifactRef(registry=_fs_registry(tmp_path), path="bad.txt", config=load_policy())
    bad = reg.run("marker", None, task, ref_bad, "studio-ok:z")
    assert not bad.passed and "not found" in bad.reason


def test_unknown_check_name_errors_clearly():
    reg = default_registry()
    task = _task("work.task", {})
    ref = ArtifactRef(registry=ToolRegistry(), path=None)
    with pytest.raises(UnknownChecker) as exc:
        reg.run("does_not_exist", None, task, ref, None)
    assert "does_not_exist" in str(exc.value)
    assert "marker" in str(exc.value)  # lists the registered checkers


# --- A vertical's domain checker: `video_audit` (the demonstration) ----------


def _video_audit(conn, task, ref, require):
    """Example DOMAIN checker a video vertical would register. It judges on FACTS
    it reads from the artifact (duration + captions), never the author's claim."""
    read = ref.read_text(task)
    content = (read.result.output or "") if (
        read.status is InvokeStatus.EXECUTED and read.result and read.result.ok) else ""
    m = re.search(r"duration_seconds:\s*(\d+)", content)
    seconds = int(m.group(1)) if m else 0
    has_captions = "captions: yes" in content
    require = require or {}
    min_seconds = int(require.get("min_seconds", 0))
    need_captions = bool(require.get("captions", False))
    facts = {"duration_seconds": seconds, "captions": has_captions}
    ok = seconds >= min_seconds and (has_captions or not need_captions)
    reason = (f"duration {seconds}s >= {min_seconds}s and captions ok" if ok
              else f"failed audit (duration {seconds}s < {min_seconds}s or captions missing)")
    return CheckResult(passed=ok, facts=facts, reason=reason)


def _video_registry() -> CheckerRegistry:
    reg = default_registry()
    reg.register("video_audit", _video_audit)
    return reg


def test_custom_checker_dispatched_by_structured_criterion_pass(tmp_path):
    (tmp_path / "clip.txt").write_text("rendered clip\nduration_seconds: 60\ncaptions: yes\n")
    task = _task("work.video", {
        "criterion": "a 60s captioned clip",
        "check": {"check": "video_audit", "require": {"min_seconds": 30, "captions": True}},
    })
    result = ExecutorResult(ok=True, artifact_path="clip.txt", invoke_status="executed")
    verdict = verify(None, task, result, MemoryEventSink(),
                     registry=_fs_registry(tmp_path), config=load_policy(),
                     checkers=_video_registry())
    assert verdict.passed
    assert verdict.facts["duration_seconds"] == 60 and verdict.facts["captions"] is True


def test_custom_checker_verdict_on_facts_beats_false_done_claim(tmp_path, monkeypatch):
    """The Executor CLAIMS ok=True and the (dry-run) model 'says done', but the
    real artifact is a 5s clip with no captions → the domain checker judges on the
    OBSERVED facts and FAILS. Evidence beats the claim (ADR-0014), on the vertical
    seam exactly as for the marker checker."""
    (tmp_path / "clip.txt").write_text("all good, trust me — done!\nduration_seconds: 5\n")

    monkeypatch.setattr(verifier_mod, "call_model",
                        lambda **kw: type("C", (), {"text": "PASS — done and verified."})())
    task = _task("work.video", {
        "check": {"check": "video_audit", "require": {"min_seconds": 30, "captions": True}},
    })
    result = ExecutorResult(ok=True, artifact_path="clip.txt", invoke_status="executed")  # false claim
    sink = MemoryEventSink()
    verdict = verify(None, task, result, sink, registry=_fs_registry(tmp_path),
                     config=load_policy(), checkers=_video_registry())
    assert not verdict.passed
    assert verdict.facts["duration_seconds"] == 5
    assert "verify.failed" in sink.types()  # learning/telemetry path still driven


def test_unknown_checker_propagates_from_verify(tmp_path):
    """A criterion naming an unregistered check surfaces the clear error through
    the Verifier (a configuration mistake, not a silent pass)."""
    (tmp_path / "art.txt").write_text("whatever\n")
    task = _task("work.task", {"check": {"check": "no_such_check"}})
    result = ExecutorResult(ok=True, artifact_path="art.txt", invoke_status="executed")
    with pytest.raises(UnknownChecker):
        verify(None, task, result, MemoryEventSink(), registry=_fs_registry(tmp_path),
               config=load_policy())


def test_marker_checker_still_default_in_verify(tmp_path):
    """No structured criterion + the default registry → the historical marker gate
    (back-compat): a matching artifact passes, a non-matching one fails."""
    (tmp_path / "art.txt").write_text("studio-ok:keep\n")
    task = _task("work.task", {"criterion": "c", "marker": "studio-ok:keep"})
    result = ExecutorResult(ok=True, artifact_path="art.txt", marker="studio-ok:keep",
                            invoke_status="executed")
    verdict = verify(None, task, result, MemoryEventSink(),
                     registry=_fs_registry(tmp_path), config=load_policy())
    assert verdict.passed and verdict.facts.get("marker") == "studio-ok:keep"
