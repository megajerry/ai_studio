"""Skill-lifecycle role + verdict tests (ADR-0024 P4).

Pure-logic tests (keep/tune/retire verdict over a fabricated efficacy report, using
the Wilson CI so a tiny sample never retires) run with NO database. Live-DB tests
exercise ``run_skill_lifecycle`` end-to-end:

- an underperforming LIVE skill (applied cohort confidently WORSE than baseline at
  n≥floor) → verdict ``retire`` → a REVIEWABLE deprecation proposal is written via the
  policy-gated filesystem tool + a body-free ``skill.deprecation_proposed`` event is
  emitted, and the LIVE skill file is NEVER touched / no auto-retire / nothing enqueued;
- a beneficial LIVE skill (applied clearly better, n≥floor) → ``keep``, NO proposal;
- a tiny-n cohort → ``insufficient``, NO proposal (never retire on thin evidence);
- the worker dispatches a ``skill_lifecycle`` task and enqueues nothing.

They SKIP cleanly when no Postgres is reachable.

    export DATABASE_URL=postgresql://aistudio@localhost:55432/aistudio
    python -m runtime.migrate
    pytest runtime/tests/test_skill_lifecycle.py
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.enforce import DbEventSink, MemoryEventSink
from runtime.event_types import (
    EVENT_SKILL_APPLIED,
    EVENT_SKILL_DEPRECATION_PROPOSED,
    EVENT_SKILL_REVISION_PROPOSED,
)
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.models import TaskStatus as S
from runtime.policy import PolicyConfig
from runtime.quality import (
    MIN_TRUSTWORTHY_SAMPLE,
    VERDICT_INSUFFICIENT,
    VERDICT_KEEP,
    VERDICT_RETIRE,
    VERDICT_REVISE,
    _rate_ci,
    skill_lifecycle_verdicts,
)
from runtime.roles.skill_lifecycle import (
    SKILL_LIFECYCLE_TASK_TYPES,
    run_skill_lifecycle,
)
from runtime.skills import SkillRegistry
from runtime.skills.models import Skill
from runtime.tasks import enqueue_task, transition
from runtime.tools import FilesystemTool, ToolRegistry
from runtime.trajectory import add_step, start_trajectory
from runtime.worker import build_registry, run_once

WRITE = PolicyConfig(roles={"skill_lifecycle": frozenset(
    {Capability.FS_READ, Capability.FS_WRITE})})
READ_ONLY = PolicyConfig(roles={"skill_lifecycle": frozenset({Capability.FS_READ})})


# ===========================================================================
# Pure logic — keep/tune/retire verdict (no DB)
# ===========================================================================


def _fam(family, a_succ, a_n, b_succ, b_n, deltas=(0.0, 0.0, 0.0)):
    """One (skill, task_type family) applied-vs-baseline block, minimal efficacy shape.

    ``deltas`` = (iterations, input_tokens, tool_search_calls) applied-minus-baseline
    means (NEGATIVE = the applied cohort explored less = an efficiency benefit).
    """
    return {
        "task_family": family,
        "applied": {"first_pass_merge_rate": _rate_ci(a_succ, a_n)},
        "baseline": {"first_pass_merge_rate": _rate_ci(b_succ, b_n)},
        "delta": {
            "iterations_mean": deltas[0],
            "input_tokens_mean": deltas[1],
            "tool_search_calls_mean": deltas[2],
            "first_pass_merge_rate": round(a_succ / a_n - b_succ / b_n, 4),
        },
    }


def _report(skill, fams, applied_count=0, workstream="w"):
    return {
        "workstream": workstream,
        "by_skill": [{"skill": skill, "applied_task_count": applied_count,
                      "by_task_family": fams}],
    }


def _entry(verdicts, name):
    return next((s for s in verdicts["by_skill"] if s["skill"] == name), None)


def test_retire_on_confident_first_pass_degradation():
    # applied 6/40 = 0.15 confidently below baseline 38/40 = 0.95; no efficiency
    # benefit (deltas 0) → a degradation whose CI excludes ≥0 → RETIRE.
    rep = _report("s", [_fam("work", 6, 40, 38, 40, deltas=(0.0, 0.0, 0.0))])
    v = skill_lifecycle_verdicts(rep, min_sample=30)
    e = _entry(v, "s")
    assert e["verdict"] == VERDICT_RETIRE
    assert e["driving_family"] == "work"
    assert v["verdicts"][VERDICT_RETIRE] == 1
    # Rigor: the applied CI upper bound really is below the baseline CI lower bound.
    fv = e["by_family"][0]
    assert fv["applied_first_pass_ci95"][1] < fv["baseline_first_pass_ci95"][0]


def test_keep_on_efficiency_benefit_even_with_equal_outcome():
    # Equal outcome (30/40 both) but the applied cohort explored LESS on all axes.
    rep = _report("s", [_fam("work", 30, 40, 30, 40, deltas=(-4.0, -400.0, -3.0))])
    v = skill_lifecycle_verdicts(rep, min_sample=30)
    assert _entry(v, "s")["verdict"] == VERDICT_KEEP


def test_keep_on_confident_outcome_improvement():
    # applied 38/40 confidently above baseline 6/40, no efficiency delta → KEEP.
    rep = _report("s", [_fam("work", 38, 40, 6, 40, deltas=(0.0, 0.0, 0.0))])
    assert _entry(skill_lifecycle_verdicts(rep, min_sample=30), "s")["verdict"] == VERDICT_KEEP


def test_revise_on_no_benefit_at_trustworthy_sample():
    # Trustworthy sample, equal outcome (30/40 both), NO exploration reduction
    # (deltas 0) → not harmful, not helping → REVISE (tune).
    rep = _report("s", [_fam("work", 30, 40, 30, 40, deltas=(0.0, 0.0, 0.0))])
    e = _entry(skill_lifecycle_verdicts(rep, min_sample=30), "s")
    assert e["verdict"] == VERDICT_REVISE


def test_tiny_sample_is_insufficient_never_retire():
    # applied 3/3 perfect-but-tiny vs baseline 1/1 → n < floor → INSUFFICIENT.
    rep = _report("s", [_fam("work", 3, 3, 1, 1, deltas=(0.0, 0.0, 0.0))])
    e = _entry(skill_lifecycle_verdicts(rep, min_sample=30), "s")
    assert e["verdict"] == VERDICT_INSUFFICIENT
    assert MIN_TRUSTWORTHY_SAMPLE == 30


def test_only_live_skills_are_judged():
    rep = _report("candidate-skill", [_fam("work", 6, 40, 38, 40)])
    # Restrict to a DIFFERENT live-skill set → the applied skill is not judged.
    v = skill_lifecycle_verdicts(rep, live_skills={"a-live-skill"}, min_sample=30)
    assert v["skills_judged"] == 0 and v["by_skill"] == []
    # Judging it explicitly (it is in the live set) → retire.
    v2 = skill_lifecycle_verdicts(rep, live_skills={"candidate-skill"}, min_sample=30)
    assert _entry(v2, "candidate-skill")["verdict"] == VERDICT_RETIRE


def test_strongest_concern_drives_per_skill_verdict():
    # One family retires, another keeps → the per-skill verdict is the worst (retire).
    rep = _report("s", [
        _fam("worka", 6, 40, 38, 40, deltas=(0.0, 0.0, 0.0)),   # retire
        _fam("workb", 30, 40, 30, 40, deltas=(-5.0, -5.0, -5.0)),  # keep
    ])
    e = _entry(skill_lifecycle_verdicts(rep, min_sample=30), "s")
    assert e["verdict"] == VERDICT_RETIRE and e["driving_family"] == "worka"


def test_none_safe_on_empty_report():
    v = skill_lifecycle_verdicts({"workstream": "w", "by_skill": []}, min_sample=30)
    assert v["skills_judged"] == 0 and v["by_skill"] == []
    assert v["verdicts"] == {VERDICT_RETIRE: 0, VERDICT_REVISE: 0,
                             VERDICT_KEEP: 0, VERDICT_INSUFFICIENT: 0}


# ===========================================================================
# Live DB — judge live skills → propose (reviewable) for underperformers
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


@pytest.fixture
def ws() -> str:
    return f"skilllc-{uuid4().hex[:12]}"


def _lifecycle_task(ws: str, **payload) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type="skill_lifecycle",
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload=payload)


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    return reg


def _live_registry(name: str, path: str) -> SkillRegistry:
    """A registry with ONE LIVE (reviewed:true) skill named ``name`` at ``path``."""
    return SkillRegistry([Skill(
        name=name, description=f"live skill {name}", reviewed=True,
        source="in-repo", path=path)])


def _seed(conn, ws, ttype, skill, *, steps, in_tokens, tools, outcome="first_pass"):
    """One task in family ``ttype`` with KNOWN efficiency + a chosen review outcome.

    ``outcome`` = "first_pass" (merged clean) | "rework" (merged after a review
    round-trip → terminal but NOT first-pass). ``skill`` (or None) attributes usage.
    """
    tid = start_trajectory(conn, "executor", ws, f"do {ttype}")
    for i in range(steps):
        add_step(conn, tid, "plan", f"step {i}")
    t = enqueue_task(conn, workstream=ws, type=ttype, payload={}, trajectory_id=tid)
    if outcome == "first_pass":
        seq = (S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    else:  # rework: a review round-trip (reviewer_blocked) → merged, not first-pass
        seq = (S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.REVIEWER_BLOCKED,
               S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    for st in seq:
        assert transition(conn, t.id, st) is not None
    if skill is not None:
        append_event(conn, make_event(
            workstream=ws, type=EVENT_SKILL_APPLIED, task_id=t.id,
            payload={"skills": [skill], "role": "executor"}))
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=t.id,
        payload={"input_tokens": in_tokens, "output_tokens": 0, "cost_usd": 0}))
    for _ in range(tools):
        append_event(conn, make_event(
            workstream=ws, type="tool.invoked", task_id=t.id, payload={}))
    append_event(conn, make_event(
        workstream=ws, type="verify.passed", task_id=t.id, payload={}))
    conn.commit()
    return t


def _seed_underperformer(conn, ws, skill):
    """Applied cohort CONFIDENTLY worse than baseline (both n=40), equal exploration.

    Applied: 6 first-pass + 34 rework merges → first-pass 6/40 = 0.15.
    Baseline (no skill): 38 first-pass + 2 rework → 0.95. Same steps/tokens/tools so
    there is NO efficiency benefit → the only signal is the outcome degradation.
    """
    for _ in range(6):
        _seed(conn, ws, "work.a", skill, steps=4, in_tokens=200, tools=3)
    for _ in range(34):
        _seed(conn, ws, "work.a", skill, steps=4, in_tokens=200, tools=3, outcome="rework")
    for _ in range(38):
        _seed(conn, ws, "work.a", None, steps=4, in_tokens=200, tools=3)
    for _ in range(2):
        _seed(conn, ws, "work.a", None, steps=4, in_tokens=200, tools=3, outcome="rework")


@pytestmark_db
def test_underperforming_live_skill_proposes_deprecation_never_touches_live(conn, ws, tmp_path):
    skill = "define-success-criteria"
    # A real live skill file on disk (the registry points at it); we assert it is
    # NEVER edited or removed by the lifecycle role.
    live_dir = Path(tempfile.mkdtemp())
    live_file = live_dir / "SKILL.md"
    original = "---\nname: define-success-criteria\nreviewed: true\n---\nLIVE BODY\n"
    live_file.write_text(original)

    _seed_underperformer(conn, ws, skill)
    sink = MemoryEventSink()
    result = run_skill_lifecycle(
        conn, _lifecycle_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
        skills=_live_registry(skill, str(live_file)),
        min_sample=30,
    )

    # The live skill was judged and a deprecation proposed.
    assert result.skills_judged == 1
    assert result.verdicts[VERDICT_RETIRE] == 1
    assert result.proposals_made == 1
    p = result.proposals[0]
    assert p.skill == skill and p.verdict == VERDICT_RETIRE and p.action == "deprecate"
    assert p.applied_first_pass_rate == 0.15 and p.baseline_first_pass_rate == 0.95
    assert p.applied_n == 40 and p.baseline_n == 40

    # A REVIEWABLE proposal artifact was written via the policy-gated tool (under the
    # confined tool root — NOT the live skills root).
    assert p.proposal_status == "executed"
    assert p.proposal_path == "proposals/skills/define-success-criteria.deprecate.md"
    written = Path(tmp_path) / p.proposal_path
    assert written.exists()
    body = written.read_text()
    assert "reviewed: false" in body and "NOT applied" in body

    # THE LIVE SKILL FILE WAS NEVER TOUCHED (unchanged; still reviewed:true).
    assert live_file.read_text() == original

    # Body-free deprecation event; never auto-retired; carries n + CI + rate/delta.
    types = sink.types()
    assert types.count(EVENT_SKILL_DEPRECATION_PROPOSED) == 1
    assert EVENT_SKILL_REVISION_PROPOSED not in types
    ev = next(e for e in sink.events if e.type == EVENT_SKILL_DEPRECATION_PROPOSED)
    assert ev.payload["skill"] == skill and ev.payload["verdict"] == VERDICT_RETIRE
    assert ev.payload["auto_retired"] is False and ev.payload["reviewed"] is False
    assert ev.payload["applied_n"] == 40 and ev.payload["applied_ci95"] is not None
    assert ev.payload["applied_first_pass_rate"] == 0.15
    # No instruction body / prompt / secret ever travels on the wire.
    for e in sink.events:
        blob = str(e.payload)
        assert "LIVE BODY" not in blob and "prompt" not in blob and "SECRET" not in blob

    # No loop: the role enqueued NOTHING (only the seeded cohort tasks exist).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream=%s AND type='skill_lifecycle'", (ws,))
        assert int(cur.fetchone()["n"]) == 0
    conn.commit()


@pytestmark_db
def test_beneficial_live_skill_is_kept_nothing_proposed(conn, ws, tmp_path):
    skill = "define-success-criteria"
    # Applied cohort explores LESS and merges clean; baseline explores more. → keep.
    for _ in range(34):
        _seed(conn, ws, "work.a", skill, steps=2, in_tokens=100, tools=1)
    for _ in range(34):
        _seed(conn, ws, "work.a", None, steps=6, in_tokens=500, tools=4)

    sink = MemoryEventSink()
    result = run_skill_lifecycle(
        conn, _lifecycle_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
        skills=_live_registry(skill, "/nonexistent/SKILL.md"), min_sample=30,
    )
    assert result.skills_judged == 1
    assert result.verdicts[VERDICT_KEEP] == 1
    assert result.proposals_made == 0 and result.proposals == []
    assert sink.types() == []                              # nothing proposed → nothing emitted
    assert not (Path(tmp_path) / "proposals").exists()     # nothing written


@pytestmark_db
def test_tiny_sample_is_insufficient_no_proposal(conn, ws, tmp_path):
    skill = "define-success-criteria"
    for _ in range(3):  # n=3 applied < floor → never retire on thin evidence
        _seed(conn, ws, "review", skill, steps=1, in_tokens=50, tools=1, outcome="rework")
    _seed(conn, ws, "review", None, steps=4, in_tokens=200, tools=3)

    sink = MemoryEventSink()
    result = run_skill_lifecycle(
        conn, _lifecycle_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
        skills=_live_registry(skill, "/nonexistent/SKILL.md"), min_sample=30,
    )
    assert result.verdicts[VERDICT_INSUFFICIENT] == 1
    assert result.proposals_made == 0
    assert sink.types() == []


@pytestmark_db
def test_without_fs_write_proposal_denied_but_event_still_emitted(conn, ws, tmp_path):
    skill = "define-success-criteria"
    _seed_underperformer(conn, ws, skill)
    sink = MemoryEventSink()
    result = run_skill_lifecycle(
        conn, _lifecycle_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=READ_ONLY,
        skills=_live_registry(skill, "/nonexistent/SKILL.md"), min_sample=30,
    )
    p = result.proposals[0]
    assert p.proposal_status == "denied" and p.proposal_path is None
    assert not (Path(tmp_path) / "proposals").exists()  # nothing written
    assert EVENT_SKILL_DEPRECATION_PROPOSED in sink.types()  # telemetry still emitted


@pytestmark_db
def test_candidate_skill_not_judged_when_not_live(conn, ws, tmp_path):
    # A skill applied but NOT in the live (reviewed) registry is never judged.
    _seed_underperformer(conn, ws, "some-candidate")
    sink = MemoryEventSink()
    result = run_skill_lifecycle(
        conn, _lifecycle_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
        skills=_live_registry("a-different-live-skill", "/x/SKILL.md"), min_sample=30,
    )
    assert result.skills_judged == 0 and result.proposals_made == 0
    assert sink.types() == []


# ===========================================================================
# Worker dispatch
# ===========================================================================


@pytestmark_db
def test_worker_dispatches_skill_lifecycle_and_enqueues_nothing(conn, ws, tmp_path):
    assert "skill_lifecycle" in SKILL_LIFECYCLE_TASK_TYPES
    skill = "define-success-criteria"
    _seed_underperformer(conn, ws, skill)

    # Enqueue a real lifecycle task and let run_once claim + dispatch it.
    enqueue_task(conn, workstream=ws, type="skill_lifecycle", payload={})
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream=%s", (ws,))
        before = int(cur.fetchone()["n"])
    conn.commit()

    registry = build_registry(str(tmp_path))
    result = run_once(
        conn, "w-test", DbEventSink(conn),
        registry=registry, config=WRITE,
        skills=_live_registry(skill, "/nonexistent/SKILL.md"),
        workstream=ws,
    )
    assert result is not None
    assert result.kind == "skill_lifecycle" and result.outcome == "done"
    assert "define-success-criteria:deprecate" in result.detail

    # The lifecycle task merged; the role enqueued NOTHING (no new tasks appeared).
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream=%s", (ws,))
        assert int(cur.fetchone()["n"]) == before
        cur.execute(
            "SELECT count(*) AS n FROM events WHERE workstream=%s AND type=%s",
            (ws, EVENT_SKILL_DEPRECATION_PROPOSED))
        assert int(cur.fetchone()["n"]) == 1
    conn.commit()
