"""Skill Curator tests (ADR-0024 P2 — induce → propose reviewable candidate).

Pure-logic tests (cluster qualification: recurring AND mature AND efficient, gated
on the Wilson CI LOWER bound + sample floor so a tiny/lucky sample never fires) run
with NO database. Live-DB tests exercise ``run_curator`` end-to-end:

- a RECURRING + MATURE (n ≥ floor, Wilson-lower-bound > floor) + EFFICIENT (below the
  family median on all exploration proxies) cluster of CLOSED trajectories is induced
  into a ``reviewed: false`` candidate ``SKILL.md`` written via the policy-gated
  filesystem tool → a body-free ``skill.proposed`` event is emitted; a non-qualifying
  (inefficient) cluster in the same family proposes NOTHING; the live ``skills/`` root
  is untouched; no skill is ever ``reviewed: true``; nothing is enqueued (no loop);
- without ``fs.write`` the candidate write is DENIED cleanly (still emits the event);
- a tiny / immature / non-recurring / inefficient cluster does NOT propose.

They SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from runtime import db
from runtime.capabilities import Capability
from runtime.enforce import MemoryEventSink
from runtime.event_types import EVENT_SKILL_PROPOSED
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.models import TaskStatus as S
from runtime.policy import PolicyConfig
from runtime.roles.curator import (
    DEFAULT_MATURITY_FLOOR,
    DEFAULT_MIN_CLUSTER_SIZE,
    cluster_report,
    detect_candidates,
    render_candidate_skill,
    run_curator,
)
from runtime.tasks import enqueue_task, transition
from runtime.tools import FilesystemTool, ToolRegistry
from runtime.trajectory import add_step, close_trajectory, start_trajectory

WRITE = PolicyConfig(roles={"curator": frozenset(
    {Capability.FS_READ, Capability.FS_WRITE})})
READ_ONLY = PolicyConfig(roles={"curator": frozenset({Capability.FS_READ})})


# ===========================================================================
# Pure logic — cluster qualification (no DB)
# ===========================================================================


def _cluster(family, sig, *, n_tasks, n_terminal, first_pass, iters, intok, tools):
    return {
        "task_family": family,
        "step_signature": list(sig),
        "task_types": [f"{family}.a"],
        "n_tasks": n_tasks,
        "n_terminal": n_terminal,
        "first_pass": first_pass,
        "iterations": {"mean": iters, "n": n_tasks},
        "input_tokens": {"mean": intok, "n": n_tasks},
        "tool_search_calls": {"mean": tools, "n": n_tasks},
    }


# Family medians the efficiency gate compares against (typical "work" cost).
_MEDIANS = {"work": {"iterations": 4.0, "input_tokens": 300.0, "tool_search_calls": 2.5}}


def _report(*clusters, medians=None):
    return {"workstream": "t", "clusters": list(clusters),
            "family_medians": medians if medians is not None else _MEDIANS}


def test_detects_recurring_mature_efficient_cluster():
    # 32 first-pass merges (rate 1.0, n≥30 → Wilson lower > floor), below the family
    # median on all three exploration proxies → a genuine reusable procedure.
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=32, n_terminal=32, first_pass=32,
                  iters=3.0, intok=100.0, tools=1.0)
    cands = detect_candidates(_report(cl))
    assert len(cands) == 1
    c = cands[0]
    assert c.task_family == "work" and c.step_signature == ["observe", "plan", "commit"]
    assert c.n_tasks == 32 and c.first_pass_rate == 1.0
    assert c.ci95[0] > DEFAULT_MATURITY_FLOOR       # fired on the CI lower bound
    assert c.efficiency_axes_below == 3
    assert c.slug.startswith("work-")


def test_tiny_sample_never_fires_even_at_rate_1():
    # 3/3 = a perfect 1.0 point estimate, but n < floor and the Wilson lower bound is
    # far below the maturity floor → NOT a matured procedure (statistical-rigor fix).
    cl = _cluster("work", ("observe", "commit"),
                  n_tasks=3, n_terminal=3, first_pass=3,
                  iters=2.0, intok=50.0, tools=1.0)
    assert detect_candidates(_report(cl), min_cluster_size=1) == []


def test_immature_low_first_pass_does_not_fire():
    # Large n but a low first-pass-merge rate (10/40 = 0.25) → not mature.
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=40, n_terminal=40, first_pass=10,
                  iters=3.0, intok=100.0, tools=1.0)
    assert detect_candidates(_report(cl)) == []


def test_borderline_ci_lower_below_floor_does_not_fire():
    # 24/30 = 0.8 point estimate > floor 0.7, but the Wilson lower bound on n=30 sits
    # below 0.7 → honest guard: not enough evidence to call it matured yet.
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=30, n_terminal=30, first_pass=24,
                  iters=3.0, intok=100.0, tools=1.0)
    assert detect_candidates(_report(cl)) == []


def test_non_recurring_cluster_does_not_fire():
    # Only 2 occurrences (< min_cluster_size) → a one-off, not a reusable procedure.
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=2, n_terminal=2, first_pass=2,
                  iters=3.0, intok=100.0, tools=1.0)
    assert detect_candidates(_report(cl), min_cluster_size=DEFAULT_MIN_CLUSTER_SIZE) == []


def test_inefficient_cluster_does_not_fire():
    # Recurring + mature, but ABOVE the family median on the exploration proxies
    # (5 > 4 iters, 500 > 300 tokens, 4 > 2.5 tools) → not efficient.
    cl = _cluster("work", ("observe", "plan", "revise", "decide", "commit"),
                  n_tasks=32, n_terminal=32, first_pass=32,
                  iters=5.0, intok=500.0, tools=4.0)
    assert detect_candidates(_report(cl)) == []


def test_partially_efficient_one_axis_above_does_not_fire():
    # Below median on 2 of 3 axes but ABOVE on tool+search calls → not efficient
    # (efficiency requires BELOW the median on ALL exploration proxies).
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=32, n_terminal=32, first_pass=32,
                  iters=3.0, intok=100.0, tools=3.0)  # 3.0 > median 2.5
    cands = detect_candidates(_report(cl))
    assert cands == []


def test_render_candidate_is_reviewed_false():
    cl = _cluster("work", ("observe", "plan", "commit"),
                  n_tasks=32, n_terminal=32, first_pass=32,
                  iters=3.0, intok=100.0, tools=1.0)
    cand = detect_candidates(_report(cl))[0]
    md = render_candidate_skill(cand)
    assert "reviewed: false" in md
    assert "reviewed: true" not in md
    assert "source: curator" in md
    # The matured recurring step sequence is summarized as instructions.
    for step in ("observe", "plan", "commit"):
        assert step in md


# ===========================================================================
# Live DB — induce → propose reviewed:false candidate + skill.proposed
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
    return f"curate-{uuid4().hex[:12]}"


def _curator_task(ws: str, **payload) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type="curate",
                status=TaskStatus.IN_PROGRESS, priority=0,
                created_at=now, updated_at=now, payload=payload)


def _registry(root) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    return reg


def _seed_traj_task(conn, ws, ttype, signature, *, in_tokens, tools, rework=False):
    """One CLOSED-trajectory task in family ``ttype`` with a KNOWN step-type signature
    and exploration metrics. ``rework=True`` routes it through a reviewer_blocked
    round-trip (so it is NOT a first-pass merge)."""
    tid = start_trajectory(conn, "executor", ws, f"do {ttype}")
    for st in signature:
        add_step(conn, tid, st, f"reasoning {st}")
    close_trajectory(conn, tid, outcome_summary="done")
    t = enqueue_task(conn, workstream=ws, type=ttype, payload={}, trajectory_id=tid)
    if rework:
        lifecycle = (S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.REVIEWER_BLOCKED,
                     S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    else:
        lifecycle = (S.CLAIMED, S.IN_PROGRESS, S.READY_FOR_REVIEW, S.APPROVED, S.MERGED)
    for st in lifecycle:
        assert transition(conn, t.id, st) is not None
    append_event(conn, make_event(
        workstream=ws, type="model.call", task_id=t.id,
        payload={"input_tokens": in_tokens, "output_tokens": 0, "cost_usd": 0}))
    for _ in range(tools):
        append_event(conn, make_event(
            workstream=ws, type="tool.invoked", task_id=t.id, payload={}))
    conn.commit()
    return t


# The two contrasting clusters in ONE "work" family: an EFFICIENT matured procedure
# and an INEFFICIENT (longer / costlier) one. The family median falls between them, so
# only the efficient cluster is below median on all three proxies.
_EFFICIENT_SIG = ["observe", "plan", "commit"]                       # 3 steps
_INEFFICIENT_SIG = ["observe", "plan", "revise", "decide", "commit"]  # 5 steps


def _seed_two_clusters(conn, ws):
    for tt in ("work.a", "work.b"):
        for _ in range(16):
            _seed_traj_task(conn, ws, tt, _EFFICIENT_SIG, in_tokens=100, tools=1)
            _seed_traj_task(conn, ws, tt, _INEFFICIENT_SIG, in_tokens=500, tools=4)


@pytestmark_db
def test_qualifying_cluster_proposes_reviewed_false_candidate(conn, ws, tmp_path):
    _seed_two_clusters(conn, ws)  # 32 efficient + 32 inefficient, all first-pass merged
    sink = MemoryEventSink()

    result = run_curator(
        conn, _curator_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=WRITE,
    )

    # Exactly ONE candidate: the efficient cluster. The inefficient one (also
    # recurring + mature) is NOT proposed — it is above the family median.
    assert result.clusters_examined == 2
    assert result.candidates_detected == 1
    cand = result.candidates[0]
    assert cand.task_family == "work"
    assert cand.step_signature == _EFFICIENT_SIG
    assert sorted(cand.task_types) == ["work.a", "work.b"]  # pooled within the family
    assert cand.n_tasks == 32 and cand.first_pass_rate == 1.0
    assert cand.efficiency_axes_below == 3

    # A REVIEWABLE candidate SKILL.md was written via the policy-gated tool, under the
    # confined candidates path — NOT the live skills/ root — and is reviewed:false.
    assert cand.proposal_status == "executed"
    assert cand.reviewed is False
    assert cand.proposal_path == f"candidates/skills/{cand.slug}/SKILL.md"
    written = Path(tmp_path) / cand.proposal_path
    assert written.exists()
    body = written.read_text()
    assert "reviewed: false" in body and "reviewed: true" not in body

    # The live skills/ root is untouched: the tool root is tmp_path, and the ONLY
    # top-level entry written there is the confined candidates/ tree.
    assert not (Path(tmp_path) / "skills").exists()
    assert [p.name for p in Path(tmp_path).iterdir()] == ["candidates"]

    # Body-free skill.proposed emitted; never auto-adopted / reviewed.
    proposed = [e for e in sink.events if e.type == EVENT_SKILL_PROPOSED]
    assert len(proposed) == 1
    p = proposed[0].payload
    assert p["candidate_slug"] == cand.slug and p["source"] == "curator"
    assert p["cluster_size"] == 32 and p["step_signature"] == _EFFICIENT_SIG
    assert p["first_pass_rate"] == 1.0 and p["ci95"][0] > DEFAULT_MATURITY_FLOOR
    assert p["reviewed"] is False and p["auto_adopted"] is False
    assert p["efficiency_axes_below_median"] == 3
    # No trajectory body / rationale / skill instructions ever travel.
    for e in sink.events:
        blob = str(e.payload)
        assert "reasoning " not in blob     # the step summaries we seeded
        assert "SKILL" not in blob and "instructions" not in blob

    # No loop: the curator enqueued NOTHING beyond the 64 seeded tasks.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM tasks WHERE workstream = %s", (ws,))
        assert int(cur.fetchone()["n"]) == 64
    conn.commit()

    # No skill anywhere was flipped to reviewed:true (candidate is the only artifact).
    assert body.count("reviewed: true") == 0


@pytestmark_db
def test_without_fs_write_candidate_is_denied_cleanly_still_emits(conn, ws, tmp_path):
    _seed_two_clusters(conn, ws)
    sink = MemoryEventSink()
    result = run_curator(
        conn, _curator_task(ws), sink,
        tool_registry=_registry(tmp_path), policy=READ_ONLY,
    )
    assert result.candidates_detected == 1
    cand = result.candidates[0]
    assert cand.proposal_status == "denied" and cand.proposal_path is None
    assert not (Path(tmp_path) / "candidates").exists()   # nothing written
    # The proposal event still fired (the induction happened; only the write is gated).
    assert EVENT_SKILL_PROPOSED in sink.types()
    assert cand.reviewed is False


@pytestmark_db
def test_only_inefficient_cluster_proposes_nothing(conn, ws, tmp_path):
    # A recurring + mature but INEFFICIENT-only family (no cheaper alternative exists,
    # so the single cluster IS the family median → not strictly below it).
    for tt in ("work.a", "work.b"):
        for _ in range(16):
            _seed_traj_task(conn, ws, tt, _INEFFICIENT_SIG, in_tokens=500, tools=4)
    sink = MemoryEventSink()
    result = run_curator(conn, _curator_task(ws), sink,
                         tool_registry=_registry(tmp_path), policy=WRITE)
    assert result.candidates_detected == 0 and result.candidates == []
    assert sink.types() == []                              # nothing induced → nothing emitted
    assert not (Path(tmp_path) / "candidates").exists()


@pytestmark_db
def test_immature_cluster_proposes_nothing(conn, ws, tmp_path):
    # An efficient + recurring cluster whose tasks mostly needed REWORK (low first-pass
    # merge) → not mature → nothing proposed. Seed a cheap baseline so it IS efficient.
    for tt in ("work.a", "work.b"):
        for _ in range(16):
            _seed_traj_task(conn, ws, tt, _EFFICIENT_SIG, in_tokens=100, tools=1, rework=True)
            _seed_traj_task(conn, ws, tt, _INEFFICIENT_SIG, in_tokens=500, tools=4)
    sink = MemoryEventSink()
    result = run_curator(conn, _curator_task(ws), sink,
                         tool_registry=_registry(tmp_path), policy=WRITE)
    assert result.candidates_detected == 0
    assert EVENT_SKILL_PROPOSED not in sink.types()


@pytestmark_db
def test_cluster_report_none_safe_on_no_closed_trajectories(conn, ws):
    rep = cluster_report(conn, ws)
    assert rep["clusters"] == [] and rep["family_medians"] == {}
    assert detect_candidates(rep) == []
