"""Skill Curator — INDUCE a candidate skill from closed trajectories → PROPOSE it
for review (ADR-0024 P2).

P0/P1 made skill USAGE attributable (``skill.applied``) and measured EFFICACY
(:func:`runtime.quality.skill_efficacy_report`). This role closes the induction
half: it reads CLOSED reasoning trajectories (:mod:`runtime.trajectory`), clusters
them by a **step-type signature** (the ordered ``step_type`` sequence) within a
**task_type family** (the leading ``.``-segment of ``tasks.type`` — P1's pooling),
recognizes when a cluster is a genuinely reusable procedure, and PROPOSES it as a
``reviewed: false`` candidate ``SKILL.md`` for a human to review.

It is PROPOSE-ONLY. It NEVER auto-adopts, NEVER flips ``reviewed: true``, and NEVER
touches the live ``skills/`` root — the candidate is written to a confined review
path via the policy-gated filesystem tool, EXACTLY as
:func:`runtime.roles.researcher._draft_candidate_skill` does. (A narrow auto-adopt
lane is a FUTURE, separate-ADR item — not built here.)

A cluster qualifies as a candidate ONLY when it is simultaneously:

- **Recurring** — it appears ≥ ``min_cluster_size`` times (a configurable
  frequency threshold); a one-off reasoning shape is not a reusable procedure.
- **Mature** — its tasks have a high first-pass-merge / low rework rate, gated
  statistically like :mod:`runtime.roles.failure_analyst`: ``n ≥ min_sample``
  (:data:`runtime.quality.MIN_TRUSTWORTHY_SAMPLE`) AND the **Wilson 95% CI lower
  bound** exceeds ``maturity_floor`` — so a perfect-but-tiny cohort (a 1.0 on n=3)
  NEVER fires.
- **Efficient** — its mean iterations / input-tokens / tool+search calls are all
  BELOW the median for its task_type family (the P1 exploration proxies): a matured
  procedure reaches the outcome with less trial-and-error than typical work.

It acts through exactly the sanctioned seams — never agent-direct (architecture §9,
CLAUDE.md invariants 1-3):

- **Reads only the append-only event log + trajectory/task tables** (no new capture,
  replayable) to build the cluster report.
- **Any file write via the policy-gated tool layer** — the candidate ``SKILL.md``
  goes through ``invoke(role="curator", tool_name="filesystem", op="write", …)`` to
  a confined review path (``candidates/skills/<slug>/SKILL.md``). A role without
  ``fs.write`` is DENIED (nothing written) — a safe, logged no-op, mirroring Sourcing.

Invariants it upholds:

- **Never auto-adopts.** The candidate is ALWAYS ``reviewed: false``; adopting it is
  a separate, human-gated step. It NEVER writes the live ``skills/`` root.
- **No loop.** A curation task enqueues nothing — it induces + proposes and stops.
- **Events leak nothing.** ``skill.proposed`` carries a candidate slug, the source,
  the cluster size, the step-type signature (structural CODES, not bodies), the
  first-pass-merge rate + n + Wilson CI, and the efficiency deltas/flags — NEVER a
  trajectory's goal/summary/rationale text or the drafted skill's instruction body
  (invariants 5 & 6, mirroring ``fix.proposed`` / ``sourcing.proposed``).
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from typing import Any, Callable, Optional

import psycopg
from pydantic import BaseModel, Field

from ..enforce import EventSink, InvokeStatus, NullEventSink, invoke
from ..event_types import EVENT_SKILL_PROPOSED
from ..models import Task, make_event
from ..policy import PolicyConfig
from ..quality import MIN_TRUSTWORTHY_SAMPLE, wilson_interval
from ..tools import ToolRegistry

log = logging.getLogger("runtime.roles.curator")

#: The queue task types the worker dispatches to :func:`run_curator`.
CURATOR_TASK_TYPES = ("curate", "skill_curation")

#: The role name the policy gate checks (must be granted ``fs.write`` to write the
#: reviewable candidate; without it the write is DENIED — a safe, logged no-op).
ROLE = "curator"

#: The three efficiency proxies (LOWER is better) compared against the family median
#: — reusing the P1 exploration proxies (:func:`runtime.quality.skill_efficacy_report`).
EFFICIENCY_METRICS = ("iterations", "input_tokens", "tool_search_calls")

#: Detection defaults. A cluster is a candidate ONLY when it clears ALL THREE gates.
#: ``min_cluster_size`` = the recurrence floor; ``maturity_floor`` = the first-pass-
#: merge Wilson-lower-bound bar; ``min_sample`` = the statistical sample floor (a rate
#: below it is never trusted, aligned with the workstream trustworthy-sample threshold).
DEFAULT_MIN_CLUSTER_SIZE = 5
DEFAULT_MATURITY_FLOOR = 0.7
DEFAULT_MIN_SAMPLE = MIN_TRUSTWORTHY_SAMPLE

#: Hard cap on candidates proposed per task — bounds fan-out (one SKILL.md each).
MAX_CANDIDATES = 4

#: Default review directory (under the confined tool root). NOT the live ``skills/``
#: root — adopting a candidate is a separate, reviewed, human-gated step.
DEFAULT_CANDIDATES_DIR = "candidates/skills"

#: A drafted candidate is ALWAYS unreviewed (review-before-use, ADR-0008). Emitted on
#: the wire so a consumer can never mistake a proposal for an adopted skill.
CANDIDATE_REVIEWED = False


# ===========================================================================
# Pure statistics + detection (no DB / no model / no network) — unit-testable
# ===========================================================================


def _share(successes: int, n: int) -> dict:
    """A rate reported HONESTLY: point estimate + n + Wilson 95% CI + a flag.

    Mirrors :func:`runtime.quality._rate_ci` using the PUBLIC
    :func:`runtime.quality.wilson_interval`, so a "1.0" on a tiny ``n`` can never be
    mistaken for a trustworthy maturity signal.
    """
    return {
        "rate": round(successes / n, 4) if n else None,
        "successes": int(successes),
        "n": int(n),
        "ci95": wilson_interval(successes, n),
        "insufficient_sample": n < MIN_TRUSTWORTHY_SAMPLE,
    }


def _is_mature(share: dict, *, floor: float, min_sample: int) -> bool:
    """A maturity test that CANNOT be tricked by a tiny sample (mirrors
    :func:`runtime.roles.failure_analyst._fires`).

    True only when ``n ≥ min_sample`` AND the Wilson 95% CI LOWER bound clears
    ``floor`` — a perfect-but-tiny first-pass-merge rate (even a 1.0 on n=3) never
    fires because its lower bound stays low.
    """
    n = int(share["n"])
    ci = share["ci95"]
    return n >= min_sample and ci is not None and ci[0] > floor


def _efficiency(cluster: dict, family_median: dict) -> tuple[dict, int]:
    """Compare a cluster's mean efficiency metrics against its family median.

    Returns ``(detail, axes_below)`` where ``detail`` maps each metric to
    ``{cluster_mean, family_median, delta, below_median}`` and ``axes_below`` counts
    how many of the three proxies are STRICTLY below the family median (a cluster is
    "efficient" only when all three are — genuinely less trial-and-error than typical).
    """
    detail: dict = {}
    axes_below = 0
    for key in EFFICIENCY_METRICS:
        cm = cluster[key]["mean"]
        med = family_median.get(key)
        below = cm is not None and med is not None and cm < med
        detail[key] = {
            "cluster_mean": cm,
            "family_median": med,
            "delta": None if (cm is None or med is None) else round(cm - med, 4),
            "below_median": below,
        }
        if below:
            axes_below += 1
    return detail, axes_below


class SkillCandidate(BaseModel):
    """A recurring + mature + efficient trajectory cluster recognized as a reusable
    procedure (the detector's output). Carries the evidence that travels on the wire.
    """

    slug: str
    task_family: str
    step_signature: list[str]      # the ordered step_type CODEs (structural, no body)
    task_types: list[str]          # the concrete tasks.type values pooled into it
    n_tasks: int                   # recurrence (cluster size)
    n_terminal: int                # maturity denominator
    first_pass: int
    first_pass_rate: Optional[float]
    ci95: Optional[tuple[float, float]]
    insufficient_sample: bool
    #: Per-metric efficiency detail (cluster_mean / family_median / delta / below).
    efficiency: dict
    efficiency_axes_below: int
    # Thresholds it cleared (for the reviewable artifact + telemetry).
    min_cluster_size: int
    maturity_floor: float
    min_sample: int


def _slug(task_family: str, step_signature: list[str]) -> str:
    """A stable, filesystem-safe slug for a (family, signature) cluster.

    ``<family>-<8hex>`` where the hex is a digest of the family + the ordered
    signature — deterministic + collision-resistant across distinct procedures.
    """
    material = f"{task_family}|{'>'.join(step_signature)}"
    return f"{task_family}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:8]}"


def detect_candidates(
    report: dict,
    *,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    maturity_floor: float = DEFAULT_MATURITY_FLOOR,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> list[SkillCandidate]:
    """Recognize reusable procedures in a :func:`cluster_report`.

    Pure + deterministic. A cluster becomes a :class:`SkillCandidate` ONLY when it
    is simultaneously **recurring** (``n_tasks ≥ min_cluster_size``), **mature**
    (:func:`_is_mature` — Wilson lower bound of first-pass-merge > ``maturity_floor``
    AND ``n_terminal ≥ min_sample``), and **efficient** (all three exploration
    proxies below the family median). Returned strongest-evidence-first (by first-
    pass-merge CI lower bound) and bounded to :data:`MAX_CANDIDATES`.
    """
    medians = report.get("family_medians", {})
    found: list[SkillCandidate] = []
    for cl in report.get("clusters", []):
        # 1. Recurring — a one-off reasoning shape is not a procedure.
        if int(cl["n_tasks"]) < min_cluster_size:
            continue
        # 2. Mature — high first-pass-merge, gated by n + Wilson lower bound.
        share = _share(int(cl["first_pass"]), int(cl["n_terminal"]))
        if not _is_mature(share, floor=maturity_floor, min_sample=min_sample):
            continue
        # 3. Efficient — below the family median on ALL exploration proxies.
        eff, axes_below = _efficiency(cl, medians.get(cl["task_family"], {}))
        if axes_below < len(EFFICIENCY_METRICS):
            continue
        found.append(
            SkillCandidate(
                slug=_slug(cl["task_family"], list(cl["step_signature"])),
                task_family=cl["task_family"],
                step_signature=list(cl["step_signature"]),
                task_types=list(cl["task_types"]),
                n_tasks=int(cl["n_tasks"]),
                n_terminal=int(cl["n_terminal"]),
                first_pass=int(cl["first_pass"]),
                first_pass_rate=share["rate"],
                ci95=share["ci95"],
                insufficient_sample=share["insufficient_sample"],
                efficiency=eff,
                efficiency_axes_below=axes_below,
                min_cluster_size=min_cluster_size,
                maturity_floor=maturity_floor,
                min_sample=min_sample,
            )
        )
    found.sort(key=lambda c: (c.ci95[0] if c.ci95 else 0.0, c.first_pass_rate or 0.0),
               reverse=True)
    return found[:MAX_CANDIDATES]


def render_candidate_skill(cand: SkillCandidate) -> str:
    """Render the reviewable ``reviewed: false`` candidate ``SKILL.md`` (frontmatter +
    body), summarizing the matured recurring procedure as instructions.

    Mirrors :func:`runtime.roles.researcher._draft_candidate_skill`: the frontmatter
    is ``reviewed: false`` + a ``source: curator`` provenance line so the inject gate
    (:func:`runtime.skills.inject.filter_injectable`) NEVER auto-injects it —
    review-before-use (ADR-0008). It is a CANDIDATE for review; adopting it is a
    separate, human-gated step.
    """
    lo, hi = cand.ci95 if cand.ci95 else (None, None)
    steps = " → ".join(f"`{s}`" for s in cand.step_signature)
    numbered = "\n".join(f"{i}. `{s}`" for i, s in enumerate(cand.step_signature, 1))
    return (
        "---\n"
        f"name: {cand.slug}\n"
        f"description: Candidate procedure induced from {cand.n_tasks} recurring, "
        f"mature, efficient trajectories in the '{cand.task_family}' task family.\n"
        f"triggers: [{cand.task_family}]\n"
        f"when_to_use: When working on '{cand.task_family}' tasks that follow this shape.\n"
        "reviewed: false\n"
        f"source: curator; family={cand.task_family}; cluster_size={cand.n_tasks}; "
        f"first_pass_rate={cand.first_pass_rate}\n"
        "---\n\n"
        f"# Induced procedure — {cand.slug} (CANDIDATE — reviewed:false, NOT adopted)\n\n"
        "Induced by the Skill Curator (ADR-0024 P2) from CLOSED reasoning trajectories\n"
        "that RECURRED, MATURED (high first-pass-merge), and ran EFFICIENTLY. REVIEW\n"
        "before use (ADR-0008); this is a reviewable candidate, never auto-adopted.\n\n"
        "## Evidence\n\n"
        f"- recurrence: {cand.n_tasks} trajectories (task_types: {', '.join(cand.task_types)})\n"
        f"- maturity: first-pass-merge {cand.first_pass_rate} "
        f"({cand.first_pass}/{cand.n_terminal}); Wilson 95% CI [{lo}, {hi}] "
        f"(lower bound > floor {cand.maturity_floor}, and n={cand.n_terminal} ≥ "
        f"floor {cand.min_sample})\n"
        f"- efficiency: below the '{cand.task_family}' family median on all of "
        "iterations / input-tokens / tool+search calls\n\n"
        "## Matured procedure (the recurring reasoning shape)\n\n"
        f"Follow this step sequence: {steps}\n\n"
        f"{numbered}\n"
    )


# ===========================================================================
# DB-integrated cluster report — read-only over trajectories + tasks + events
# ===========================================================================


def cluster_report(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> dict:
    """Cluster CLOSED trajectories by (task_type family, step-type signature).

    Read-only + replayable — derived entirely from ``trajectories`` +
    ``trajectory_steps`` + ``tasks`` + ``task_transitions`` + the append-only event
    log (no new capture). Each CLOSED trajectory that produced a task contributes one
    sample; its **step-type signature** is the ordered ``step_type`` sequence
    (``array_agg(... ORDER BY seq)``) and its **task_type family** is the leading
    ``.``-segment of ``tasks.type`` (matching P1's pooling). Samples sharing both form
    a cluster.

    For each cluster it reports the recurrence (``n_tasks``), the maturity counts
    (``n_terminal`` / ``first_pass``), and the mean exploration proxies (iterations =
    trajectory steps, input_tokens = summed ``model.call`` input, tool_search_calls =
    ``tool.invoked`` + ``search.provider_call``) — the P1 metrics. It also reports the
    per-family MEDIAN of each proxy (over all the family's clustered tasks) so
    :func:`detect_candidates` can judge efficiency relative to typical family work.
    ``workstream=None`` spans all workstreams. None-safe on no closed trajectories.
    """
    params = {"ws": workstream}
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scope AS (
              SELECT t.id AS task_id,
                     split_part(t.type, '.', 1) AS task_family,
                     t.type AS task_type,
                     t.status,
                     tr.id AS trajectory_id
              FROM tasks t
              JOIN trajectories tr ON tr.id = t.trajectory_id
              WHERE tr.status = 'closed'
                AND (%(ws)s::text IS NULL OR t.workstream = %(ws)s::text)
            ),
            sig AS (
              SELECT trajectory_id,
                     array_agg(step_type ORDER BY seq) AS step_signature,
                     count(*) AS n_steps
              FROM trajectory_steps GROUP BY trajectory_id
            ),
            model AS (
              SELECT task_id,
                     COALESCE(sum((payload->>'input_tokens')::bigint), 0) AS input_tokens
              FROM events
              WHERE type='model.call' AND task_id IS NOT NULL
                AND (%(ws)s::text IS NULL OR workstream = %(ws)s::text)
              GROUP BY task_id
            ),
            toolsearch AS (
              SELECT task_id, count(*) AS n_calls
              FROM events
              WHERE type IN ('tool.invoked','search.provider_call') AND task_id IS NOT NULL
                AND (%(ws)s::text IS NULL OR workstream = %(ws)s::text)
              GROUP BY task_id
            ),
            rework AS (
              SELECT task_id, bool_or(
                  to_status='reviewer_blocked'
                  OR (from_status IN ('ready_for_review','reviewer_blocked')
                      AND to_status='in_progress')
              ) AS had_rework
              FROM task_transitions GROUP BY task_id
            )
            SELECT s.task_id, s.task_family, s.task_type, s.status,
                   sg.step_signature,
                   COALESCE(sg.n_steps, 0)        AS n_steps,
                   COALESCE(m.input_tokens, 0)    AS input_tokens,
                   COALESCE(ts.n_calls, 0)        AS tool_search_calls,
                   COALESCE(rw.had_rework, false) AS had_rework
            FROM scope s
            JOIN sig sg ON sg.trajectory_id = s.trajectory_id
            LEFT JOIN model m       ON m.task_id = s.task_id
            LEFT JOIN toolsearch ts ON ts.task_id = s.task_id
            LEFT JOIN rework rw     ON rw.task_id = s.task_id
            """,
            params,
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()

    # Group rows into clusters keyed by (family, signature); track family-wide values.
    clusters: dict[tuple[str, tuple[str, ...]], dict] = {}
    family_values: dict[str, dict[str, list]] = {}
    for r in rows:
        sig = tuple(r["step_signature"] or ())
        if not sig:  # a trajectory with no steps is not a procedure — skip
            continue
        fam = r["task_family"]
        iters = int(r["n_steps"])
        in_tok = int(r["input_tokens"])
        tools = int(r["tool_search_calls"])
        terminal = r["status"] in ("merged", "abandoned")
        first_pass = r["status"] == "merged" and not bool(r["had_rework"])

        fv = family_values.setdefault(
            fam, {"iterations": [], "input_tokens": [], "tool_search_calls": []}
        )
        fv["iterations"].append(iters)
        fv["input_tokens"].append(in_tok)
        fv["tool_search_calls"].append(tools)

        key = (fam, sig)
        cl = clusters.setdefault(
            key,
            {
                "task_family": fam,
                "step_signature": list(sig),
                "task_types": set(),
                "n_tasks": 0,
                "n_terminal": 0,
                "first_pass": 0,
                "_iterations": [],
                "_input_tokens": [],
                "_tool_search_calls": [],
            },
        )
        cl["task_types"].add(r["task_type"])
        cl["n_tasks"] += 1
        cl["n_terminal"] += 1 if terminal else 0
        cl["first_pass"] += 1 if first_pass else 0
        cl["_iterations"].append(iters)
        cl["_input_tokens"].append(in_tok)
        cl["_tool_search_calls"].append(tools)

    def _mean(values: list) -> Optional[float]:
        return round(sum(values) / len(values), 4) if values else None

    out_clusters: list[dict] = []
    for cl in clusters.values():
        out_clusters.append({
            "task_family": cl["task_family"],
            "step_signature": cl["step_signature"],
            "task_types": sorted(cl["task_types"]),
            "n_tasks": cl["n_tasks"],
            "n_terminal": cl["n_terminal"],
            "first_pass": cl["first_pass"],
            "iterations": {"mean": _mean(cl["_iterations"]), "n": len(cl["_iterations"])},
            "input_tokens": {"mean": _mean(cl["_input_tokens"]), "n": len(cl["_input_tokens"])},
            "tool_search_calls": {"mean": _mean(cl["_tool_search_calls"]),
                                  "n": len(cl["_tool_search_calls"])},
        })
    # Deterministic order (family, then signature) — stable output for tests/telemetry.
    out_clusters.sort(key=lambda c: (c["task_family"], c["step_signature"]))

    family_medians = {
        fam: {k: (round(statistics.median(v[k]), 4) if v[k] else None)
              for k in EFFICIENCY_METRICS}
        for fam, v in family_values.items()
    }

    return {
        "workstream": workstream,
        "clusters": out_clusters,
        "family_medians": family_medians,
    }


# ===========================================================================
# Result model
# ===========================================================================


class ProposedSkill(BaseModel):
    """One recognized cluster → its ``reviewed: false`` candidate SKILL.md proposal."""

    slug: str
    task_family: str
    step_signature: list[str]
    task_types: list[str] = Field(default_factory=list)
    n_tasks: int
    n_terminal: int
    first_pass_rate: Optional[float]
    ci95: Optional[tuple[float, float]]
    insufficient_sample: bool
    efficiency_axes_below: int
    #: Candidate-write outcome: "off" | "executed" | "denied" | "pending".
    proposal_status: str = "off"
    #: Path (tool-root-relative) of the written candidate, if written.
    proposal_path: Optional[str] = None
    #: A proposed candidate is ALWAYS unreviewed (review-before-use, ADR-0008).
    reviewed: bool = CANDIDATE_REVIEWED


class CurationResult(BaseModel):
    """What one curation task produced (returned to the worker as the result).

    Counts / slugs / rates only — never trajectory bodies or skill instructions
    (invariants 5 & 6). ``candidates_detected`` is 0 when no cluster qualifies
    (nothing proposed, nothing emitted).
    """

    workstream: str
    clusters_examined: int
    candidates_detected: int
    candidates: list[ProposedSkill] = Field(default_factory=list)
    min_cluster_size: int
    maturity_floor: float
    min_sample: int


# ===========================================================================
# DB-integrated role — induce → propose reviewable candidate
# ===========================================================================


def _write_candidate(
    conn: Any,
    task: Task,
    content: str,
    path: str,
    *,
    tool_registry: ToolRegistry,
    policy: Optional[PolicyConfig],
    sink: EventSink,
    invoke_fn: Callable[..., Any],
) -> tuple[str, Optional[str]]:
    """Write the reviewable ``reviewed: false`` candidate via the policy-gated fs tool.

    Returns ``(status, path)``; a role without ``fs.write`` is DENIED (nothing
    written) — a safe, logged no-op, mirroring :func:`runtime.roles.researcher`.
    """
    result = invoke_fn(
        role=ROLE,
        tool_name="filesystem",
        registry=tool_registry,
        config=policy,
        events=sink,
        conn=conn,
        workstream=task.workstream,
        task_id=task.id,
        op="write",
        path=path,
        content=content,
    )
    status = getattr(result.status, "value", str(result.status))
    wrote = result.status is InvokeStatus.EXECUTED and bool(
        result.result and getattr(result.result, "ok", False)
    )
    return status, (path if wrote else None)


def run_curator(
    conn: Any,
    task: Task,
    sink: Optional[EventSink] = None,
    *,
    tool_registry: Optional[ToolRegistry] = None,
    policy: Optional[PolicyConfig] = None,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    maturity_floor: float = DEFAULT_MATURITY_FLOOR,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    candidates_dir: str = DEFAULT_CANDIDATES_DIR,
    build_report: Callable[..., dict] = cluster_report,
    invoke_fn: Callable[..., Any] = invoke,
) -> CurationResult:
    """Service one curation task: induce candidate skills → propose them for review.

    Builds a :func:`cluster_report` for ``task.workstream``, recognizes reusable
    procedures (:func:`detect_candidates` — recurring AND mature AND efficient), and
    for each (bounded to :data:`MAX_CANDIDATES`): writes a ``reviewed: false``
    candidate ``SKILL.md`` to the confined ``candidates/skills/<slug>/`` review path
    via the policy-gated filesystem tool, and emits a body-free ``skill.proposed``
    event (slug / source / cluster size / step-type signature / first-pass-merge rate
    + n + Wilson CI / efficiency deltas).

    It NEVER auto-adopts, NEVER flips ``reviewed: true``, NEVER writes the live
    ``skills/`` root, and enqueues NOTHING (no loop). ``min_cluster_size`` /
    ``maturity_floor`` / ``min_sample`` may be overridden from ``task.payload``.
    Injectable seams (``build_report`` / ``invoke_fn``) keep it testable; ``policy``
    gates the candidate write.
    """
    sink = sink or NullEventSink()
    payload = task.payload or {}
    min_cluster_size = int(payload.get("min_cluster_size", min_cluster_size))
    maturity_floor = float(payload.get("maturity_floor", maturity_floor))
    min_sample = int(payload.get("min_sample", min_sample))

    report = build_report(conn, task.workstream)
    candidates = detect_candidates(
        report,
        min_cluster_size=min_cluster_size,
        maturity_floor=maturity_floor,
        min_sample=min_sample,
    )

    proposals: list[ProposedSkill] = []
    for cand in candidates:
        # 1. Write the reviewable reviewed:false candidate SKILL.md via the policy-gated
        #    tool (never the live skills/ root). Denied cleanly without fs.write.
        proposal_status = "off"
        proposal_path: Optional[str] = None
        if tool_registry is not None:
            content = render_candidate_skill(cand)
            proposal_status, proposal_path = _write_candidate(
                conn, task, content, f"{candidates_dir}/{cand.slug}/SKILL.md",
                tool_registry=tool_registry, policy=policy, sink=sink, invoke_fn=invoke_fn,
            )

        # 2. Emit body-free skill.proposed — slug / source / structural signature /
        #    evidence only. NEVER a trajectory body or the skill's instruction text.
        sink.emit(
            make_event(
                workstream=task.workstream,
                type=EVENT_SKILL_PROPOSED,
                task_id=task.id,
                payload={
                    "candidate_slug": cand.slug,
                    "source": "curator",
                    "task_family": cand.task_family,
                    "step_signature": cand.step_signature,  # step_type CODEs (structural)
                    "step_count": len(cand.step_signature),
                    "task_types": cand.task_types,
                    "cluster_size": cand.n_tasks,
                    "n_terminal": cand.n_terminal,
                    "first_pass_rate": cand.first_pass_rate,
                    "ci95": list(cand.ci95) if cand.ci95 else None,
                    "insufficient_sample": cand.insufficient_sample,
                    "efficiency_axes_below_median": cand.efficiency_axes_below,
                    "efficiency": {
                        k: {"delta": v["delta"], "below_median": v["below_median"]}
                        for k, v in cand.efficiency.items()
                    },
                    "min_cluster_size": min_cluster_size,
                    "maturity_floor": maturity_floor,
                    "min_sample": min_sample,
                    "proposal_written": bool(proposal_path),
                    "reviewed": CANDIDATE_REVIEWED,   # invariant: never adopted here
                    "auto_adopted": False,            # invariant: never auto-adopted
                },
            )
        )

        proposals.append(
            ProposedSkill(
                slug=cand.slug,
                task_family=cand.task_family,
                step_signature=cand.step_signature,
                task_types=cand.task_types,
                n_tasks=cand.n_tasks,
                n_terminal=cand.n_terminal,
                first_pass_rate=cand.first_pass_rate,
                ci95=cand.ci95,
                insufficient_sample=cand.insufficient_sample,
                efficiency_axes_below=cand.efficiency_axes_below,
                proposal_status=proposal_status,
                proposal_path=proposal_path,
                reviewed=CANDIDATE_REVIEWED,
            )
        )

    return CurationResult(
        workstream=task.workstream,
        clusters_examined=len(report.get("clusters", [])),
        candidates_detected=len(candidates),
        candidates=proposals,
        min_cluster_size=min_cluster_size,
        maturity_floor=maturity_floor,
        min_sample=min_sample,
    )
