"""Trajectory-level eval — judge a PERSISTED PM decision (harness v2).

Harness v1 scored the PM's plan *structure* (shape, DAG, criteria). This eval scores
the PM's *decision quality* by reading a PERSISTED reasoning trajectory from the
``trajectories`` / ``trajectory_steps`` tables (ADR-0020) and handing it to the
**swappable judge** (:mod:`evals.judge`) against the ``pm_decision_quality`` rubric.
Today the judge runs on the dryrun provider (deterministic, keyless, mechanism
signal); at go-live a real model judges the very same trajectory item with zero code
change.

This is the "real-outcome eval on real persisted state" seam the stakeholder asked
to have in place now: the plumbing (read trajectory → build rubric item → judge →
verdict with CI-aware roll-up) is complete and exercised; only the *judging model*
is a stand-in until keys land.

:func:`seed_demo_trajectory` writes one well-formed PM trajectory so the eval always
has something real to score (the test seeds its own; the harness auto-seeds in a
throwaway ``eval-traj-*`` workstream, mirroring how the PM eval writes throwaway
telemetry).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from runtime.trajectory import (
    add_step,
    close_trajectory,
    get_trajectory,
    list_steps,
    start_trajectory,
)

from .corpus import Rubric, load_rubric
from .judge import Judge, JudgeVerdict

#: The rubric this eval judges a PM trajectory against (from the corpus).
PM_DECISION_RUBRIC_ID = "pm_decision_quality"


def build_trajectory_item(traj: Any, steps: list) -> dict:
    """Project a persisted trajectory + its steps into an ID-FREE judge item.

    Deliberately excludes UUIDs / timestamps so the dryrun judge's deterministic
    score is stable across runs (the seeded trajectory has fresh ids each run). The
    real model would read the same content-only view. Carries the causal fields the
    ``pm_decision_quality`` rubric asks about: goal, steps (type/summary/options/
    choice/confidence), and the outcome summary."""
    return {
        "goal": traj.goal,
        "num_steps": len(steps),
        "outcome_summary": traj.outcome_summary,
        "steps": [
            {
                "step_type": s.step_type,
                "summary": s.summary,
                "options_considered": s.options_considered,
                "choice": s.choice,
                "confidence": s.confidence,
            }
            for s in steps
        ],
    }


@dataclass
class TrajectoryEvalResult:
    """Outcome of judging one persisted PM trajectory against the rubric."""

    trajectory_id: str
    rubric_id: str
    verdict: JudgeVerdict
    item: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Honest gate: on a **dryrun** judge, ``passed`` means the MECHANISM ran
        (a deterministic verdict was produced) — a dryrun model cannot judge real
        quality. On a **real** judge, ``passed`` is the model's actual verdict."""
        return True if self.verdict.dry_run else self.verdict.passed

    def to_dict(self) -> dict:
        return {
            "name": "pm_trajectory_decision_quality",
            "description": (
                "LLM-as-judge decision-quality score of a PERSISTED PM trajectory "
                "(swappable judge; dryrun=mechanism, real model=outcome at go-live)."
            ),
            "trajectory_id": self.trajectory_id,
            "rubric_id": self.rubric_id,
            "verdict": self.verdict.to_dict(),
            "item": self.item,
            "passed": self.passed,
        }


def seed_demo_trajectory(
    conn: Any, *, workstream: Optional[str] = None, now: Optional[datetime] = None
) -> UUID:
    """Persist one well-formed PM reasoning trajectory and return its id.

    A realistic decision episode: observe → plan → consult → decide → decompose →
    commit, with options weighed and a confident choice, then closed with an
    outcome. Written to a throwaway ``eval-traj-*`` workstream so it never collides
    with real work."""
    ws = workstream or f"eval-traj-{uuid4().hex[:8]}"
    now = now or datetime.now(timezone.utc)
    tid = start_trajectory(
        conn, "pm", ws,
        "Launch a weekly short-form video channel with captioned clips.",
        context_size_start=1200, now=now,
    )
    add_step(conn, tid, "observe",
             "Reviewed the goal and current studio capacity.",
             rationale="One-person studio; video pipeline is dry-run only so far.",
             now=now)
    add_step(conn, tid, "plan",
             "Sketched a 3-part plan: script, produce captioned clip, publish.",
             rationale="Splitting isolates the caption-quality risk into its own item.",
             now=now)
    add_step(conn, tid, "consult",
             "Critic flagged caption accuracy as the top quality risk.",
             rationale="Captions are the acceptance criterion the audience notices.",
             now=now)
    add_step(conn, tid, "decide",
             "Chose the captioned-clip-first plan over a raw-upload shortcut.",
             rationale="Captions drive reach and accessibility; worth the extra step.",
             options_considered=["captioned-clip-first", "raw-upload shortcut",
                                 "outsource editing"],
             choice="captioned-clip-first", confidence=0.82, now=now)
    add_step(conn, tid, "decompose",
             "Emitted 3 work items with per-item success criteria.",
             refs={"num_items": 3}, now=now)
    add_step(conn, tid, "commit",
             "Committed the plan to the task queue.",
             choice="proceed", confidence=0.82, now=now)
    close_trajectory(conn, tid,
                     outcome_summary="Plan committed: 3 captioned-clip work items queued.",
                     now=now)
    return tid


def cleanup_trajectory_shape(conn: Any, workstream: str) -> None:
    """Delete every throwaway row seeded under the ``eval-traj-*`` ``workstream``.

    Mirrors :func:`evals.grounding_eval.cleanup_grounding_shape`: scoped to this
    seeder's OWN workstream only (never a global truncate). Deleting the trajectory
    cascades its ``trajectory_steps`` (``ON DELETE CASCADE``); the body-free
    ``trajectory.*`` events emitted under the same workstream are removed too."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM trajectories WHERE workstream LIKE %s",
                    (f"{workstream}%",))
        cur.execute("DELETE FROM events WHERE workstream LIKE %s", (f"{workstream}%",))
    conn.commit()


def run_trajectory_eval(
    conn: Any,
    trajectory_id: Optional[UUID] = None,
    *,
    judge: Optional[Judge] = None,
    rubric: Optional[Rubric] = None,
    workstream: str = "eval-judge",
    keep: bool = False,
) -> TrajectoryEvalResult:
    """Read a persisted PM trajectory and score its decision quality via the judge.

    ``trajectory_id=None`` seeds a demo trajectory first (so the harness always has
    real state to score) — that throwaway ``eval-traj-*`` episode is deleted after
    the run (unless ``keep=True``) so orphans don't accumulate in the shared DB. A
    ``trajectory_id`` passed IN is left untouched (we didn't create it). Needs a
    live ``conn`` (reads the trajectory tables + the judge's model.call telemetry
    lands through it); keyless via the dryrun judge."""
    seeded_ws: Optional[str] = None
    if trajectory_id is None:
        seeded_ws = f"eval-traj-{uuid4().hex[:8]}"
        trajectory_id = seed_demo_trajectory(conn, workstream=seeded_ws)
    try:
        traj = get_trajectory(conn, trajectory_id)
        if traj is None:
            raise ValueError(f"trajectory {trajectory_id} not found")
        steps = list_steps(conn, trajectory_id)

        rubric = rubric or load_rubric(PM_DECISION_RUBRIC_ID)
        judge = judge or Judge()
        item = build_trajectory_item(traj, steps)
        verdict = judge.score(rubric, item, conn=conn, workstream=workstream)

        return TrajectoryEvalResult(
            trajectory_id=str(trajectory_id),
            rubric_id=rubric.id,
            verdict=verdict,
            item=item,
        )
    finally:
        # Only clean up what WE seeded; never a caller-supplied (real) trajectory.
        if seeded_ws is not None and not keep:
            cleanup_trajectory_shape(conn, seeded_ws)


__all__ = [
    "PM_DECISION_RUBRIC_ID",
    "TrajectoryEvalResult",
    "build_trajectory_item",
    "seed_demo_trajectory",
    "cleanup_trajectory_shape",
    "run_trajectory_eval",
]
