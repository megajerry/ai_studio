"""Skill review queue + human-gated promote tests (ADR-0024 P3).

Pure-logic tests (scan both sources, None-safety, per-slug fingerprint, gate-flip)
run with NO database. Live-DB tests exercise the 🔴 review gate end-to-end:

- ``scan_candidates`` lists ``reviewed: false`` candidates from BOTH the Curator and
  the Researcher with the correct source + provenance, joining the Curator's
  ``skill.proposed`` evidence; an already-adopted (reviewed:true) file is excluded;
- ``promote_candidate`` is 🔴-gated: with NO approved request it adopts NOTHING (no
  file in the live skills root, no ``reviewed: true``, no ``skill.adopted``); WITH an
  approved request it adopts → the live skills root ``reviewed: true`` + a body-free
  ``skill.adopted`` (both branches proven);
- a grant for one slug NEVER authorizes another (per-slug fingerprint);
- the Retro → ``curate`` handoff is queue-only (enqueues a ``curate`` task, never a
  direct call), and only for a clean first-pass WORK success.

They SKIP cleanly when no Postgres is reachable.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from runtime import db
from runtime.approvals import pending_approvals, resolve_approval
from runtime.capabilities import Capability
from runtime.enforce import MemoryEventSink
from runtime.event_types import (
    EVENT_RETRO_COMPLETED,
    EVENT_SKILL_ADOPTED,
    EVENT_SKILL_PROPOSED,
)
from runtime.events import append_event
from runtime.migrate import migrate
from runtime.models import Task, TaskStatus, make_event
from runtime.policy import PolicyConfig
from runtime.roles.retro import RETRO_TASK_TYPE, run_retro
from runtime.skills.review_queue import (
    PROMOTER_ROLE,
    Candidate,
    adopt_fingerprint,
    promote_candidate,
    scan_candidates,
)
from runtime.skills.review_queue import _mark_reviewed
from runtime.tools import FilesystemTool, ToolRegistry

# Promoter policy: fs.read + fs.write (the 🔴 gate is the human approval, not the tier).
PROMOTER_POLICY = PolicyConfig(
    roles={PROMOTER_ROLE: frozenset({Capability.FS_READ, Capability.FS_WRITE})}
)
READ_ONLY_POLICY = PolicyConfig(roles={PROMOTER_ROLE: frozenset({Capability.FS_READ})})


# ---------------------------------------------------------------------------
# Candidate fixtures — exactly what the Curator / Researcher write.
# ---------------------------------------------------------------------------

_CURATOR_MD = """---
name: work-abc12345
description: Candidate procedure induced from 32 recurring, mature, efficient trajectories.
triggers: [work]
when_to_use: When working on 'work' tasks that follow this shape.
reviewed: false
source: curator; family=work; cluster_size=32; first_pass_rate=1.0
---

# Induced procedure — work-abc12345 (CANDIDATE — reviewed:false, NOT adopted)

Follow this step sequence: `observe` -> `plan` -> `commit`
"""

_RESEARCHER_MD = """---
name: llm-agents
description: Candidate best-practice for 'llm agents', drafted by the Researcher.
triggers: [llm-agents]
when_to_use: When working on 'llm agents'.
reviewed: false
source: researcher; topic_hash=deadbeefcafe0001; sources=5
---

# llm agents

Drafted from external research. REVIEW before use (ADR-0008).

- Consult the gathered best-practice before starting a task in this area.
"""

_ADOPTED_MD = """---
name: already-adopted
description: A skill that has already been reviewed + adopted.
reviewed: true
source: curator; family=work
---

# already adopted
"""


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _seed_candidates(scratch: Path) -> Path:
    """Two candidates from BOTH sources (+ one already-adopted) under candidates/."""
    cand = scratch / "candidates"
    _write(cand, "skills/work-abc12345/SKILL.md", _CURATOR_MD)   # curator layout
    _write(cand, "llm-agents/SKILL.md", _RESEARCHER_MD)           # researcher layout
    _write(cand, "skills/already-adopted/SKILL.md", _ADOPTED_MD)  # excluded (reviewed)
    return cand


def _skills_registry(root: Path) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(FilesystemTool(root=str(root)))
    return reg


# ===========================================================================
# Pure logic — scan + gate helpers (no DB)
# ===========================================================================


def test_scan_none_safe():
    assert scan_candidates(None) == []
    assert scan_candidates("/does/not/exist/anywhere") == []


def test_scan_lists_both_sources_and_excludes_adopted(tmp_path):
    cand = _seed_candidates(tmp_path)
    cands = scan_candidates(cand)  # conn=None → frontmatter-only evidence
    by_slug = {c.slug: c for c in cands}
    # Only the two reviewed:false candidates; the reviewed:true one is excluded.
    assert set(by_slug) == {"work-abc12345", "llm-agents"}
    assert all(c.reviewed is False for c in cands)

    cur = by_slug["work-abc12345"]
    assert cur.source == "curator"
    assert cur.provenance.startswith("curator;")
    assert cur.path.endswith("skills/work-abc12345/SKILL.md")

    res = by_slug["llm-agents"]
    assert res.source == "researcher"
    assert res.provenance.startswith("researcher;")
    # Researcher evidence is parsed from the provenance line (no per-slug event).
    assert res.evidence.get("sources") == 5
    assert res.evidence.get("topic_hash") == "deadbeefcafe0001"


def test_scan_excludes_already_adopted_slugs(tmp_path):
    cand = _seed_candidates(tmp_path)
    # Simulate work-abc12345 already adopted into the live skills root (reviewed:true).
    live = tmp_path / "live_skills"
    _write(live, "work-abc12345/SKILL.md", _mark_reviewed(_CURATOR_MD))
    slugs = {c.slug for c in scan_candidates(cand, live_root=live)}
    assert slugs == {"llm-agents"}  # the adopted one drops off the queue
    # Without live_root it is still listed (candidate file lingers).
    assert {c.slug for c in scan_candidates(cand)} == {"work-abc12345", "llm-agents"}


def test_scan_skips_malformed_candidate(tmp_path):
    cand = tmp_path / "candidates"
    _write(cand, "good/SKILL.md", _RESEARCHER_MD)
    _write(cand, "bad/SKILL.md", "no frontmatter here at all")
    cands = scan_candidates(cand)
    assert [c.slug for c in cands] == ["llm-agents"]  # bad one skipped, not fatal


def test_adopt_fingerprint_is_per_slug_and_workstream():
    assert adopt_fingerprint("a", "w1") == adopt_fingerprint("a", "w1")   # stable
    assert adopt_fingerprint("a", "w1") != adopt_fingerprint("b", "w1")   # per-slug
    assert adopt_fingerprint("a", "w1") != adopt_fingerprint("a", "w2")   # per-workstream


def test_mark_reviewed_flips_only_the_gate_line():
    out = _mark_reviewed(_CURATOR_MD)
    assert "reviewed: true" in out
    assert "reviewed: false" not in out
    # Body preserved (only the gate line changed).
    assert "observe` -> `plan` -> `commit" in out
    with pytest.raises(ValueError):
        _mark_reviewed(out)  # no 'reviewed: false' line left to flip


# ===========================================================================
# Live DB — the 🔴 review gate + evidence join + retro handoff
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
    return f"review-{uuid4().hex[:12]}"


@pytestmark_db
def test_scan_joins_curator_proposed_evidence(conn, ws, tmp_path):
    cand = _seed_candidates(tmp_path)
    # A real body-free skill.proposed event the Curator would have emitted.
    append_event(conn, make_event(
        workstream=ws, type=EVENT_SKILL_PROPOSED,
        payload={
            "candidate_slug": "work-abc12345", "source": "curator",
            "task_family": "work", "cluster_size": 32, "n_terminal": 32,
            "first_pass_rate": 1.0, "ci95": [0.89, 1.0], "insufficient_sample": False,
            "efficiency_axes_below_median": 3, "step_count": 3,
        }))
    conn.commit()

    cands = {c.slug: c for c in scan_candidates(cand, conn=conn, workstream=ws)}
    ev = cands["work-abc12345"].evidence
    assert ev["cluster_size"] == 32 and ev["first_pass_rate"] == 1.0
    assert ev["ci95"] == [0.89, 1.0] and ev["efficiency_axes_below_median"] == 3
    assert cands["work-abc12345"].source == "curator"
    # Researcher candidate still present with frontmatter evidence (no event).
    assert cands["llm-agents"].evidence.get("sources") == 5


@pytestmark_db
def test_promote_is_red_gated_pending_then_adopt(conn, ws, tmp_path):
    cand_root = _seed_candidates(tmp_path)
    live_skills = tmp_path / "live_skills"
    reg = _skills_registry(live_skills)
    slug = "work-abc12345"
    sink = MemoryEventSink()

    # --- Branch 1: NO approved request → adopts NOTHING (pends on a 🔴 request). ---
    r1 = promote_candidate(
        conn, slug, "human:jerry",
        candidates_root=cand_root, skills_tool_registry=reg,
        policy=PROMOTER_POLICY, sink=sink, workstream=ws,
    )
    assert r1.status == "pending" and r1.approval_id is not None
    assert r1.reviewed is False and r1.adopted_path is None
    # NOTHING written to the live skills root; NO skill.adopted emitted.
    assert not live_skills.exists() or list(live_skills.rglob("SKILL.md")) == []
    assert EVENT_SKILL_ADOPTED not in sink.types()
    # A real pending 🔴 approval exists for this action.
    pend = [a for a in pending_approvals(conn) if str(a.id) == r1.approval_id]
    assert len(pend) == 1 and pend[0].tier == "red"

    # Idempotent: promoting again before approval does NOT auto-adopt (still pending).
    r_again = promote_candidate(
        conn, slug, "human:jerry",
        candidates_root=cand_root, skills_tool_registry=reg,
        policy=PROMOTER_POLICY, sink=sink, workstream=ws,
    )
    assert r_again.status == "pending" and r_again.approval_id == r1.approval_id
    assert list(live_skills.rglob("SKILL.md")) == [] if live_skills.exists() else True

    # --- A human APPROVES the request (turns it into a one-shot grant). ---
    approved = resolve_approval(conn, UUID(r1.approval_id), "approved", "human:jerry", sink, workstream=ws)
    assert approved is not None and approved.status == "approved"

    # --- Branch 2: WITH the approved request → adopts into the live skills root. ---
    sink2 = MemoryEventSink()
    r2 = promote_candidate(
        conn, slug, "human:jerry",
        candidates_root=cand_root, skills_tool_registry=reg,
        policy=PROMOTER_POLICY, sink=sink2, workstream=ws,
    )
    assert r2.status == "adopted" and r2.reviewed is True
    adopted = live_skills / slug / "SKILL.md"
    assert adopted.exists()
    body = adopted.read_text()
    assert "reviewed: true" in body and "reviewed: false" not in body
    # The candidate itself is UNTOUCHED (still reviewed:false — it is not the artifact).
    cand_file = cand_root / "skills" / slug / "SKILL.md"
    assert "reviewed: false" in cand_file.read_text()

    # A body-free skill.adopted was emitted (slug/source/approver/flags — no body).
    adopted_events = [e for e in sink2.events if e.type == EVENT_SKILL_ADOPTED]
    assert len(adopted_events) == 1
    p = adopted_events[0].payload
    assert p["slug"] == slug and p["source"] == "curator"
    assert p["approver"] == "human:jerry" and p["reviewed"] is True
    assert p["auto_adopted"] is False
    for e in sink2.events:
        blob = str(e.payload)
        assert "observe" not in blob and "Induced procedure" not in blob

    # The grant is one-shot: a THIRD promote finds no live grant → pends again.
    r3 = promote_candidate(
        conn, slug, "human:jerry",
        candidates_root=cand_root, skills_tool_registry=reg,
        policy=PROMOTER_POLICY, sink=MemoryEventSink(), workstream=ws,
    )
    assert r3.status == "pending"


@pytestmark_db
def test_grant_for_one_slug_does_not_authorize_another(conn, ws, tmp_path):
    cand_root = _seed_candidates(tmp_path)
    live_skills = tmp_path / "live_skills"
    reg = _skills_registry(live_skills)
    sink = MemoryEventSink()

    # Approve a grant for the curator candidate ONLY.
    r = promote_candidate(conn, "work-abc12345", "human", candidates_root=cand_root,
                          skills_tool_registry=reg, policy=PROMOTER_POLICY, sink=sink, workstream=ws)
    resolve_approval(conn, UUID(r.approval_id), "approved", "human", sink, workstream=ws)

    # Promoting the OTHER candidate must NOT be authorized by that grant → pending.
    other = promote_candidate(conn, "llm-agents", "human", candidates_root=cand_root,
                              skills_tool_registry=reg, policy=PROMOTER_POLICY,
                              sink=MemoryEventSink(), workstream=ws)
    assert other.status == "pending"
    assert not (live_skills / "llm-agents" / "SKILL.md").exists()


@pytestmark_db
def test_promote_missing_candidate_is_not_found(conn, ws, tmp_path):
    cand_root = _seed_candidates(tmp_path)
    reg = _skills_registry(tmp_path / "live_skills")
    r = promote_candidate(conn, "does-not-exist", "human", candidates_root=cand_root,
                          skills_tool_registry=reg, policy=PROMOTER_POLICY,
                          sink=MemoryEventSink(), workstream=ws)
    assert r.status == "not_found"
    # A not-found candidate raises NO approval (nothing to review).
    assert pending_approvals(conn) == [] or all(
        a.reason.find("does-not-exist") == -1 for a in pending_approvals(conn)
    )


# --- Retro → curate handoff (queue-only, dual-source wiring) -----------------


def _retro_task(ws: str, target_id, outcome: str) -> Task:
    now = datetime.now(timezone.utc)
    return Task(id=uuid4(), workstream=ws, type=RETRO_TASK_TYPE, status=TaskStatus.IN_PROGRESS,
                priority=0, created_at=now, updated_at=now,
                payload={"target_task_id": str(target_id), "target_task_type": "work.demo",
                         "outcome": outcome})


@pytestmark_db
def test_retro_enqueues_curate_on_clean_work_success(conn, ws):
    target_id = uuid4()
    for typ in ("executor.acted", "verify.passed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id))
    conn.commit()

    captured: list = []

    def fake_enqueue(conn, *, workstream, type, payload=None, priority=0, **kw):
        captured.append({"workstream": workstream, "type": type, "payload": payload})
        return types.SimpleNamespace(id=uuid4())

    sink = MemoryEventSink()
    res = run_retro(conn, _retro_task(ws, target_id, "done"), sink, enqueue=fake_enqueue)

    # Queue-only handoff: exactly ONE curate task enqueued (no direct curator call).
    assert res.crystallize_enqueued is True and res.curate_task_id is not None
    assert len(captured) == 1
    assert captured[0]["type"] == "curate" and captured[0]["workstream"] == ws
    assert captured[0]["payload"]["trigger"] == "retro"
    # retro.completed carries the curate task id (body-free id only).
    completed = [e for e in sink.events if e.type == EVENT_RETRO_COMPLETED]
    assert completed and completed[0].payload["curate_task_id"] == res.curate_task_id


@pytestmark_db
def test_retro_does_not_enqueue_curate_on_failure(conn, ws):
    target_id = uuid4()
    for typ in ("executor.acted", "verify.failed", "task.finished"):
        append_event(conn, make_event(workstream=ws, type=typ, task_id=target_id))
    conn.commit()

    captured: list = []

    def fake_enqueue(conn, **kw):
        captured.append(kw)
        return types.SimpleNamespace(id=uuid4())

    sink = MemoryEventSink()
    res = run_retro(conn, _retro_task(ws, target_id, "failed"), sink, enqueue=fake_enqueue)
    assert res.crystallize_enqueued is False and res.curate_task_id is None
    assert captured == []
    # No curate id leaked into retro.completed.
    completed = [e for e in sink.events if e.type == EVENT_RETRO_COMPLETED]
    assert completed and "curate_task_id" not in completed[0].payload
