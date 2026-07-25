"""Unified skill candidate review queue + the human-gated promote path (ADR-0024 P3).

P0/P1/P2 made skill usage attributable, measured efficacy, and grew TWO sources of
``reviewed: false`` candidate ``SKILL.md`` files:

- the **Researcher** (:func:`runtime.roles.researcher._draft_candidate_skill`) —
  a candidate distilled from EXTERNAL best-practice, written to
  ``candidates/<slug>/SKILL.md`` under the confined tool root;
- the **Curator** (:func:`runtime.roles.curator.run_curator`) — a candidate INDUCED
  from a recurring + mature + efficient trajectory cluster, written to
  ``candidates/skills/<slug>/SKILL.md`` and announced with a body-free
  ``skill.proposed`` event.

This module completes ``propose → review → adopt`` (ADR-0008 review-before-use):

1. :func:`scan_candidates` — a READ-ONLY, None-safe reader that scans the candidates
   path for ``reviewed: false`` candidate skills from BOTH sources and joins their
   proposal events into ONE list (slug, source, provenance, evidence summary).
2. :func:`promote_candidate` — the 🔴 human review gate. It adopts a candidate into
   the live ``skills/`` root with ``reviewed: true`` **only** on a human-APPROVED
   request (reusing :mod:`runtime.approvals`): with no live grant it raises a real
   :func:`runtime.approvals.request_approval` and adopts NOTHING; with a grant it
   consumes it (one-shot), writes the adopted skill via the policy-gated filesystem
   tool, and emits a body-free ``skill.adopted``. It NEVER auto-promotes and NEVER
   flips ``reviewed: true`` without an approved request (the narrow auto-adopt lane
   is a FUTURE, separate-ADR item — not built here).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Optional, Union

from pydantic import BaseModel, Field

from ..approvals import (
    compute_fingerprint,
    consume_grant as _consume_grant,
    find_grant as _find_grant,
    request_approval as _request_approval,
)
from ..capabilities import ActionTier
from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke as _invoke
from ..event_types import EVENT_SKILL_ADOPTED, EVENT_SKILL_PROPOSED
from ..models import make_event
from ..policy import PolicyConfig
from ..tools import ToolRegistry
from .loader import load_skill
from .models import SkillError

log = logging.getLogger("runtime.skills.review_queue")

#: The two known candidate sources (the frontmatter ``source:`` line leads with one).
SOURCE_CURATOR = "curator"
SOURCE_RESEARCHER = "researcher"
SOURCE_UNKNOWN = "unknown"

#: The role the policy gate checks for the adopt write (granted ``fs.write``; the
#: 🔴 gate is the human approval below, NOT the tool tier — the write itself is 🟡).
PROMOTER_ROLE = "skill_promoter"

#: The adopt action's approval tier — a 🔴 (RED) human decision (ADR-0008/0006).
ADOPT_APPROVAL_TIER = ActionTier.RED.value

#: Synthetic tool label folded into the adopt fingerprint (stable + PER-SLUG, so a
#: grant authorizes EXACTLY one candidate's adoption, never another's).
_ADOPT_TOOL = "skill.adopt"


class Candidate(BaseModel):
    """One ``reviewed: false`` candidate skill awaiting human review (both sources).

    Carries identity + provenance + an evidence summary — never the instruction body
    (the reviewer opens the file at ``path`` to read it). ``evidence`` holds the
    joined proposal facts (n + Wilson CI / efficiency deltas for the Curator; source
    count for the Researcher) so a reviewer can triage without opening every file.
    """

    slug: str
    source: str = SOURCE_UNKNOWN
    #: Absolute path of the candidate ``SKILL.md`` (so a reviewer can open it).
    path: str
    reviewed: bool = False
    description: str = ""
    triggers: list[str] = Field(default_factory=list)
    #: The raw ``source:`` frontmatter line (full provenance string).
    provenance: str = ""
    #: Evidence summary joined from the proposal event / frontmatter (may be empty).
    evidence: dict = Field(default_factory=dict)


class PromoteResult(BaseModel):
    """Outcome of one :func:`promote_candidate` call (counts/ids/flags only)."""

    slug: str
    #: "not_found" | "pending" | "adopted" | "write_failed".
    status: str
    source: str = SOURCE_UNKNOWN
    #: Set when the review gate raised (or reused) a 🔴 approval request (id only).
    approval_id: Optional[str] = None
    #: Path the adopted skill was written to in the live skills root, if adopted.
    adopted_path: Optional[str] = None
    #: True only after a human-approved adoption flipped the gate to reviewed:true.
    reviewed: bool = False


# ===========================================================================
# Read-only queue — scan candidates + join their proposal events
# ===========================================================================


def _parse_source(source_field: str) -> str:
    """The leading token of a candidate's ``source:`` frontmatter line.

    Both writers lead with the source name (``curator; family=…`` /
    ``researcher; topic_hash=…``). Returns ``curator`` / ``researcher`` / ``unknown``.
    """
    head = (source_field or "").split(";", 1)[0].strip().split()
    tok = head[0].lower() if head else ""
    if tok == SOURCE_CURATOR:
        return SOURCE_CURATOR
    if tok == SOURCE_RESEARCHER:
        return SOURCE_RESEARCHER
    return SOURCE_UNKNOWN


def _researcher_evidence(source_field: str) -> dict:
    """Best-effort evidence for a Researcher candidate parsed from its source line.

    The Researcher emits no per-slug event, so its evidence lives in the frontmatter
    provenance (``sources=N``; ``topic_hash=…``). None of these are bodies.
    """
    ev: dict = {}
    m = re.search(r"sources=(\d+)", source_field or "")
    if m:
        ev["sources"] = int(m.group(1))
    m = re.search(r"topic_hash=([0-9a-f]+)", source_field or "")
    if m:
        ev["topic_hash"] = m.group(1)
    return ev


def _proposed_evidence(payload: dict) -> dict:
    """Distil the evidence summary from a ``skill.proposed`` payload (Curator).

    Carries the statistically-honest maturity signal (n + Wilson CI) + the efficiency
    deltas — the facts a reviewer weighs. All are structural facts, never bodies.
    """
    return {
        "cluster_size": payload.get("cluster_size"),
        "n_terminal": payload.get("n_terminal"),
        "first_pass_rate": payload.get("first_pass_rate"),
        "ci95": payload.get("ci95"),
        "insufficient_sample": payload.get("insufficient_sample"),
        "efficiency_axes_below_median": payload.get("efficiency_axes_below_median"),
        "efficiency": payload.get("efficiency"),
        "task_family": payload.get("task_family"),
        "step_count": payload.get("step_count"),
    }


def _proposed_index(conn: Any, workstream: Optional[str]) -> dict:
    """Index the latest ``skill.proposed`` event payload by candidate slug.

    None-safe: a missing ``conn`` or any read error yields an empty index (the queue
    then reflects frontmatter-only evidence). Reads via the append-only log only.
    """
    if conn is None:
        return {}
    try:
        from ..events import read_events  # lazy: keep the module import DB-free

        events = read_events(conn, type=EVENT_SKILL_PROPOSED, workstream=workstream)
    except Exception:  # pragma: no cover - degrade to no joined evidence
        log.warning("review_queue: could not read skill.proposed events; no join", exc_info=True)
        return {}
    index: dict = {}
    for e in events:  # seq-ascending → last write per slug wins
        slug = (e.payload or {}).get("candidate_slug")
        if slug:
            index[slug] = e.payload
    return index


def _adopted_slugs(live_root: Union[str, Path, None]) -> set:
    """Slugs already ADOPTED (present as a skill) under the live skills root.

    Used to keep the queue accurate: once a candidate has been promoted into the live
    root, it is no longer a pending review item even though its candidate file may
    linger. None-safe: a missing/None root yields an empty set.
    """
    if live_root is None:
        return set()
    from .registry import SkillRegistry  # lazy: avoid an import cycle at module load

    try:
        return set(SkillRegistry.discover(live_root).names())
    except Exception:  # pragma: no cover - never let the live-root read break the scan
        log.warning("review_queue: could not read live skills root %s", live_root, exc_info=True)
        return set()


def scan_candidates(
    candidates_root: Union[str, Path, None],
    *,
    conn: Any = None,
    workstream: Optional[str] = None,
    live_root: Union[str, Path, None] = None,
) -> list[Candidate]:
    """Scan ``candidates_root`` for ``reviewed: false`` candidates from BOTH sources.

    READ-ONLY + None-safe: a missing/None root yields ``[]``; a malformed candidate
    is logged and skipped (never crashes the scan, mirroring
    :meth:`runtime.skills.SkillRegistry.discover`). Every ``SKILL.md`` under the root
    is parsed; only ``reviewed: false`` ones are returned (an already-adopted skill is
    not a review-queue item). The candidate's ``source`` is read from its frontmatter
    provenance and its evidence summary is joined from the matching ``skill.proposed``
    event (Curator) or parsed from the provenance line (Researcher). Returned sorted
    by slug for stable output.

    ``live_root`` (optional): when supplied, a candidate whose slug already exists as a
    skill under the live skills root is EXCLUDED — so a candidate that has been promoted
    (adopted) drops off the queue even if its candidate file lingers.
    """
    if candidates_root is None:
        return []
    root = Path(candidates_root).expanduser()
    if not root.exists():
        return []

    index = _proposed_index(conn, workstream)
    adopted = _adopted_slugs(live_root)
    out: list[Candidate] = []
    for skill_file in sorted(root.rglob("SKILL.md")):
        try:
            skill = load_skill(skill_file)
        except SkillError as exc:
            log.warning("review_queue: skipping malformed candidate: %s", exc)
            continue
        if skill.reviewed:  # only unreviewed candidates belong in the review queue
            continue
        if skill.name in adopted:  # already promoted into the live root — not pending
            continue
        source = _parse_source(skill.source)
        payload = index.get(skill.name)
        if payload is not None:
            evidence = _proposed_evidence(payload)
            # Trust the event's source label when present (authoritative provenance).
            source = payload.get("source", source) or source
        elif source == SOURCE_RESEARCHER:
            evidence = _researcher_evidence(skill.source)
        else:
            evidence = {}
        out.append(
            Candidate(
                slug=skill.name,
                source=source,
                path=str(skill.path or skill_file.resolve()),
                reviewed=skill.reviewed,
                description=skill.description,
                triggers=list(skill.triggers),
                provenance=skill.source,
                evidence=evidence,
            )
        )
    out.sort(key=lambda c: c.slug)
    return out


def _find_candidate(candidates_root: Union[str, Path, None], slug: str) -> Optional[Candidate]:
    """Return the (unreviewed) candidate matching ``slug``, or ``None``."""
    for cand in scan_candidates(candidates_root):
        if cand.slug == slug:
            return cand
    return None


# ===========================================================================
# Human-gated promote — the 🔴 review-gate adoption
# ===========================================================================


def adopt_fingerprint(slug: str, workstream: str = "productivity") -> str:
    """A stable, per-``(workstream, slug)`` approval fingerprint.

    Uses the shared :func:`runtime.approvals.compute_fingerprint` with the workstream
    and slug folded into the hashed material, so a grant approved for candidate A can
    never be spent to adopt candidate B, and a grant approved in one workstream can
    never authorize an adoption in another. Contains no argument values or bodies
    (invariant 5).
    """
    return compute_fingerprint(None, _ADOPT_TOOL, [f"ws={workstream}", f"slug={slug}"])


def _mark_reviewed(raw: str) -> str:
    """Flip the frontmatter ``reviewed: false`` → ``reviewed: true`` (gate satisfied).

    Operates ONLY on the review-gate line so the rest of the candidate is preserved
    byte-for-byte. Raises if there is no ``reviewed: false`` line to flip (a candidate
    must be unreviewed to be promotable — a guard against silently adopting garbage).
    """
    new, n = re.subn(r"(?mi)^(\s*reviewed:\s*)false\s*$", r"\1true", raw)
    if n == 0:
        raise ValueError("candidate has no 'reviewed: false' frontmatter line to flip")
    return new


def promote_candidate(
    conn: Any,
    slug: str,
    approver: str,
    *,
    candidates_root: Union[str, Path],
    skills_tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig] = None,
    sink: Optional[EventSink] = None,
    workstream: str = "productivity",
    role: str = PROMOTER_ROLE,
    skill_subdir: str = "",
    invoke_fn: Callable[..., Any] = _invoke,
    request_approval_fn: Callable[..., Any] = _request_approval,
    find_grant_fn: Callable[..., Any] = _find_grant,
    consume_grant_fn: Callable[..., Any] = _consume_grant,
) -> PromoteResult:
    """Adopt a candidate into the live ``skills/`` root — the 🔴 human review gate.

    The review-gate adoption (ADR-0008): this is the ONLY path that flips a candidate
    to ``reviewed: true``, and it does so ONLY on a human-approved request:

    - **No live grant** → raise (or reuse) a real 🔴 :func:`request_approval` for
      this exact slug and return ``status="pending"`` having adopted NOTHING (no file
      written to the live skills root, no ``reviewed: true``, no ``skill.adopted``).
    - **Live grant** → consume it (one-shot), read the candidate, flip its review gate
      to ``reviewed: true``, write it to the live skills root via the policy-gated
      filesystem tool (``skills_tool_registry`` must be rooted AT the live skills root),
      and emit a body-free ``skill.adopted`` (slug / source / approver / reviewed).

    It NEVER auto-promotes and NEVER flips ``reviewed: true`` without an approved
    request. ``skills_tool_registry`` supplies the confined write target; ``policy``
    gates it (``role`` needs ``fs.write``). The approval seams are injectable for tests.
    """
    sink = sink or NullEventSink()

    cand = _find_candidate(candidates_root, slug)
    if cand is None:
        return PromoteResult(slug=slug, status="not_found")

    fingerprint = adopt_fingerprint(slug, workstream)

    # Approval gate — a live one-shot grant turns the 🔴 into a single adoption.
    grant = find_grant_fn(conn, fingerprint) if conn is not None else None
    if grant is None or consume_grant_fn(conn, grant.id) is None:
        # No grant (or lost the race): persist a pending 🔴 request; adopt NOTHING.
        approval = None
        if conn is not None:
            approval = request_approval_fn(
                conn,
                task_id=None,
                role=role,
                tool="skills",
                capabilities=["fs.write"],
                tier=ADOPT_APPROVAL_TIER,
                reason=(
                    f"adopt skill candidate {slug!r} (source={cand.source}) into the "
                    "live skills/ root as reviewed:true (ADR-0008 review gate)"
                ),
                sink=sink,
                workstream=workstream,
                fingerprint=fingerprint,
            )
        return PromoteResult(
            slug=slug,
            status="pending",
            source=cand.source,
            approval_id=str(approval.id) if approval is not None else None,
        )

    # Grant consumed (one-shot) → adopt: flip the gate + write to the live skills root.
    raw = Path(cand.path).read_text(encoding="utf-8")
    adopted = _mark_reviewed(raw)
    sub = skill_subdir.strip("/")
    rel = f"{sub}/{slug}/SKILL.md" if sub else f"{slug}/SKILL.md"
    result = invoke_fn(
        role=role,
        tool_name="filesystem",
        registry=skills_tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=workstream,
        op="write",
        path=rel,
        content=adopted,
    )
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    if not wrote:
        # The grant was spent but the confined write did not land (e.g. missing
        # fs.write). Report it honestly; nothing was adopted.
        return PromoteResult(
            slug=slug,
            status="write_failed",
            source=cand.source,
            approval_id=str(grant.id),
        )

    written = rel
    if result.result is not None and result.result.metadata:
        written = result.result.metadata.get("path", rel)

    # Body-free skill.adopted — slug / source / approver / reviewed flag only.
    sink.emit(
        make_event(
            workstream=workstream,
            type=EVENT_SKILL_ADOPTED,
            task_id=None,
            payload={
                "slug": slug,
                "source": cand.source,
                "approver": approver,
                "approval_id": str(grant.id),
                "reviewed": True,
                "auto_adopted": False,  # invariant: only ever via an approved request
            },
        )
    )
    return PromoteResult(
        slug=slug,
        status="adopted",
        source=cand.source,
        approval_id=str(grant.id),
        adopted_path=written,
        reviewed=True,
    )
