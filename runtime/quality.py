"""Telemetry-driven quality/ops rollup (ADR-0012, ADR-0015).

The per-task rollups in :mod:`runtime.tasks` (``task_lifecycle`` / ``task_cost`` /
``agent_rollup`` / ``model_rollup``) answer "what did THIS task cost / how did the
fleet move". This module rolls the same append-only telemetry up to a
**workstream-level quality report** — the numbers the Retro, the Reviewer, and a
future Grafana panel read to judge how well a workstream is actually operating.

Everything is derived from the immutable event log + ``task_transitions`` (no new
capture), so it is replayable and consistent with the rest of the platform.

Metrics (each rate is ``None`` when its denominator is 0, never a divide-by-zero):

- **task_success_rate** = merged / (merged + abandoned) — of the tasks that
  reached a terminal state, the fraction that succeeded.
- **verify_pass_rate** = verify.passed / (verify.passed + verify.failed) — how
  often the independent evidence gate passed work on the first look.
- **rekick_rate** = task.rekicked / (merged + abandoned) — supervisor re-kicks
  (stalled workers) per terminal task; an operational-health signal.
- **error_rate** = (abandoned + verify.failed) / (merged + abandoned +
  verify.passed + verify.failed) — the fraction of all outcome/quality signals
  that were failures.
- **avg cost + tokens per completed task** — model spend attributable to merged
  tasks, amortized over the count of merged tasks.
- **avg latency per completed task** — summed lifecycle latency of merged tasks
  over the count of merged tasks; plus the mean per-transition latency.
- **pm_decision_quality** — OUTCOME ATTRIBUTION for PM decisions: joins each
  ``trajectory`` → the ``tasks`` it created → those tasks' lifecycle outcomes to
  score first-pass-merge / rework / escalation / abandoned rates. Every rate is
  reported with its sample size ``n``, a **Wilson 95% CI**, and an
  ``insufficient_sample`` flag (``n < 30``) so a point estimate on a tiny sample is
  never mistaken for a trustworthy signal. See :func:`pm_decision_quality`.

NOTE (honest scope): dry-run means COST/TOKENS are the router's deterministic
dry-run estimates and OUTCOME quality is not yet measured — these rollups measure
MECHANISM + ops health now, and the same functions report real spend/quality once
real models are wired at go-live. See ``docs/evaluation.md``.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import psycopg

from .tasks import model_rollup

#: Below this many outcome samples a rate is statistically untrustworthy — a point
#: estimate like "1.0" on n=3 is noise, not signal. Rates computed on fewer than
#: this many trials are flagged ``insufficient_sample=True`` (see ``_rate_ci``).
MIN_TRUSTWORTHY_SAMPLE = 30


def _ratio(num: float, den: float) -> Optional[float]:
    """``num/den`` rounded, or ``None`` when the denominator is 0 (undefined)."""
    return round(num / den, 4) if den else None


def wilson_interval(
    successes: int, n: int, z: float = 1.96
) -> Optional[tuple[float, float]]:
    """Wilson score confidence interval for a binomial proportion.

    Returns ``(lower, upper)`` (each clamped to ``[0, 1]``, rounded to 4dp) for the
    ``successes``-of-``n`` proportion at confidence level ``z`` (default ``1.96`` =
    95%), or ``None`` when ``n == 0`` (no sample → the interval is undefined). The
    Wilson interval is used instead of the naive normal approximation precisely
    because it stays sensible at the extremes (p near 0 or 1) and small ``n`` — e.g.
    5/5 does NOT give ``[1.0, 1.0]`` but ``[0.566, 1.0]``, honestly reflecting that
    a perfect-but-tiny sample is weak evidence.

    Pure/deterministic — no I/O, no DB — so it is trivially unit-testable.
    """
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} out of range for n={n}")
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return (round(lower, 4), round(upper, 4))


def _rate_ci(successes: int, n: int) -> dict:
    """A rate reported HONESTLY: point estimate + sample size + 95% CI + a flag.

    Bundles ``rate`` (``successes/n`` or ``None`` for ``n==0``), the raw
    ``successes``/``n``, the Wilson 95% ``ci95`` (``None`` for ``n==0``), and
    ``insufficient_sample`` (``True`` when ``n < MIN_TRUSTWORTHY_SAMPLE``). This is
    the anti-pattern fix: a rate never travels without its ``n`` and CI, so a "1.0"
    on ``n=3`` can't be mistaken for a trustworthy result.
    """
    return {
        "rate": _ratio(successes, n),
        "successes": int(successes),
        "n": int(n),
        "ci95": wilson_interval(successes, n),
        "insufficient_sample": n < MIN_TRUSTWORTHY_SAMPLE,
    }


#: The four PM decision-outcome axes, all reported over the same denominator
#: ``n`` = terminal (merged/abandoned) tasks the decision created.
_PM_METRICS = (
    "first_pass_merge_rate",  # merged with no reviewer_blocked / review rework
    "rework_rate",            # hit reviewer_blocked or bounced review → in_progress
    "escalation_rate",        # parked 'blocked' on a 🔴 approval at some point
    "abandoned_rate",         # reached terminal 'abandoned'
)


def _empty_pm_metrics() -> dict:
    """The four outcome rates on an empty sample (all ``n=0`` → None-safe)."""
    return {m: _rate_ci(0, 0) for m in _PM_METRICS}


def _metrics_from_counts(row: dict) -> dict:
    """Build the four ``_rate_ci`` metric blocks from one aggregated counts row.

    ``row`` carries ``n_terminal`` (the shared denominator) plus the success counts
    ``first_pass`` / ``rework`` / ``escalation`` / ``abandoned``.
    """
    n = int(row["n_terminal"])
    return {
        "first_pass_merge_rate": _rate_ci(int(row["first_pass"]), n),
        "rework_rate": _rate_ci(int(row["rework"]), n),
        "escalation_rate": _rate_ci(int(row["escalation"]), n),
        "abandoned_rate": _rate_ci(int(row["abandoned"]), n),
    }


def pm_decision_quality(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> dict:
    """Outcome-attribution rollup: PM decision quality, scored by what its tasks did.

    Joins ``trajectories`` → the ``tasks`` each created (``tasks.trajectory_id``) →
    those tasks' real lifecycle outcomes (derived from ``task_transitions`` + the
    current status, never guessed). For each decision trajectory — and pooled per
    role and overall — it reports four outcome rates, EACH with its sample size
    ``n``, a Wilson 95% CI, and an ``insufficient_sample`` flag (``n < 30``):

    - **first_pass_merge_rate** — of the tasks that reached a terminal state, the
      fraction that reached ``merged`` WITHOUT ever hitting ``reviewer_blocked`` or
      bouncing back to ``in_progress`` from review (clean first pass).
    - **rework_rate** — fraction that hit ``reviewer_blocked`` or bounced back to
      ``in_progress`` from review at least once.
    - **escalation_rate** — fraction that was parked ``blocked`` on a 🔴 approval.
    - **abandoned_rate** — fraction that reached terminal ``abandoned``.

    The denominator ``n`` for every rate is the count of TERMINAL tasks the decision
    created (merged + abandoned) — a task still in flight has no outcome yet and is
    excluded from ``n`` (but counted in ``n_tasks``). ``workstream=None`` spans all
    workstreams. Returns ``{by_trajectory, by_role, overall}``.
    """
    params = {"ws": workstream}
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH linked AS (
              SELECT tr.id AS trajectory_id, tr.role, tr.workstream,
                     t.id AS task_id, t.status
              FROM trajectories tr
              JOIN tasks t ON t.trajectory_id = tr.id
              WHERE (%(ws)s::text IS NULL OR tr.workstream = %(ws)s::text)
            ),
            flags AS (
              SELECT
                l.trajectory_id, l.role, l.workstream, l.task_id,
                (l.status IN ('merged','abandoned'))            AS is_terminal,
                (l.status = 'merged')                            AS is_merged,
                (l.status = 'abandoned')                         AS is_abandoned,
                -- rework: a review round-trip ever occurred (hit reviewer_blocked,
                -- or a bounce from review back into in_progress).
                EXISTS (
                  SELECT 1 FROM task_transitions x WHERE x.task_id = l.task_id
                    AND (x.to_status = 'reviewer_blocked'
                         OR (x.from_status IN ('ready_for_review','reviewer_blocked')
                             AND x.to_status = 'in_progress'))
                )                                                AS had_rework,
                -- escalation: parked 'blocked' on a 🔴 approval at some point.
                EXISTS (
                  SELECT 1 FROM task_transitions x WHERE x.task_id = l.task_id
                    AND x.to_status = 'blocked'
                )                                                AS had_escalation
              FROM linked l
            )
            SELECT
              trajectory_id, role, workstream,
              count(*)                                             AS n_tasks,
              count(*) FILTER (WHERE is_terminal)                  AS n_terminal,
              count(*) FILTER (WHERE is_merged AND NOT had_rework) AS first_pass,
              count(*) FILTER (WHERE is_terminal AND had_rework)   AS rework,
              count(*) FILTER (WHERE is_terminal AND had_escalation) AS escalation,
              count(*) FILTER (WHERE is_abandoned)                 AS abandoned
            FROM flags
            GROUP BY trajectory_id, role, workstream
            ORDER BY trajectory_id
            """,
            params,
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()

    by_trajectory: list[dict] = []
    # Pooled accumulators keyed by role, plus one overall bucket.
    role_acc: dict[str, dict] = {}
    overall = {k: 0 for k in ("n_tasks", "n_terminal", "first_pass",
                              "rework", "escalation", "abandoned")}
    for r in rows:
        by_trajectory.append({
            "trajectory_id": str(r["trajectory_id"]),
            "role": r["role"],
            "workstream": r["workstream"],
            "n_tasks": int(r["n_tasks"]),
            "n_terminal": int(r["n_terminal"]),
            "metrics": _metrics_from_counts(r),
        })
        acc = role_acc.setdefault(
            r["role"],
            {k: 0 for k in ("n_tasks", "n_terminal", "first_pass",
                            "rework", "escalation", "abandoned")},
        )
        for k in acc:
            acc[k] += int(r[k])
            overall[k] += int(r[k])

    by_role = {
        role: {
            "n_tasks": acc["n_tasks"],
            "n_terminal": acc["n_terminal"],
            "trajectories": sum(1 for r in rows if r["role"] == role),
            "metrics": _metrics_from_counts(acc),
        }
        for role, acc in sorted(role_acc.items())
    }

    return {
        "workstream": workstream,
        "trajectories_scored": len(rows),
        "by_trajectory": by_trajectory,
        "by_role": by_role,
        "overall": {
            "n_tasks": overall["n_tasks"],
            "n_terminal": overall["n_terminal"],
            "metrics": _metrics_from_counts(overall) if rows else _empty_pm_metrics(),
        },
    }


def quality_report(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> dict:
    """Compute the workstream quality/ops rollup from the live telemetry.

    ``workstream=None`` reports across ALL workstreams. Returns a nested dict of
    ``totals`` (raw counts), ``rates`` (the derived quality ratios), ``cost`` and
    ``latency`` (per-completed-task efficiency), and ``by_model_global`` (the reused
    :func:`runtime.tasks.model_rollup`, which is process-wide, not ws-filtered — so
    it is labeled ``_global``). Reads only the append-only event log +
    ``task_transitions``.
    """
    params = {"ws": workstream}
    with conn.cursor() as cur:
        # --- counts + spend from the event log (workstream-filterable) --------
        cur.execute(
            """
            SELECT
              count(*) FILTER (WHERE type='task.transition'
                               AND payload->>'to'='merged')     AS merged,
              count(*) FILTER (WHERE type='task.transition'
                               AND payload->>'to'='abandoned')  AS abandoned,
              count(*) FILTER (WHERE type='verify.passed')      AS verify_passed,
              count(*) FILTER (WHERE type='verify.failed')      AS verify_failed,
              count(*) FILTER (WHERE type='task.rekicked')      AS rekicks,
              count(*) FILTER (WHERE type='model.call')         AS model_calls,
              COALESCE(sum((payload->>'cost_usd')::numeric)
                       FILTER (WHERE type='model.call'), 0)     AS cost_usd,
              COALESCE(sum(((payload->>'input_tokens')::bigint
                          + (payload->>'output_tokens')::bigint))
                       FILTER (WHERE type='model.call'), 0)     AS tokens,
              COALESCE(avg((payload->>'latency_ms')::bigint)
                       FILTER (WHERE type='task.transition'), 0)::bigint
                                                                AS avg_transition_latency_ms
            FROM events
            WHERE (%(ws)s::text IS NULL OR workstream = %(ws)s::text)
            """,
            params,
        )
        agg = cur.fetchone()

        # --- per-completed-task efficiency (merged tasks only) ----------------
        cur.execute(
            """
            WITH completed AS (
              SELECT DISTINCT task_id FROM events
              WHERE type='task.transition' AND payload->>'to'='merged'
                AND task_id IS NOT NULL
                AND (%(ws)s::text IS NULL OR workstream = %(ws)s::text)
            )
            SELECT
              (SELECT count(*) FROM completed) AS n_completed,
              COALESCE((SELECT sum((e.payload->>'latency_ms')::bigint)
                        FROM events e JOIN completed c ON e.task_id = c.task_id
                        WHERE e.type='task.transition'), 0) AS completed_latency_ms,
              COALESCE((SELECT sum((e.payload->>'cost_usd')::numeric)
                        FROM events e JOIN completed c ON e.task_id = c.task_id
                        WHERE e.type='model.call'), 0) AS completed_cost_usd,
              COALESCE((SELECT sum(((e.payload->>'input_tokens')::bigint
                                  + (e.payload->>'output_tokens')::bigint))
                        FROM events e JOIN completed c ON e.task_id = c.task_id
                        WHERE e.type='model.call'), 0) AS completed_tokens
            """,
            params,
        )
        comp = cur.fetchone()
    if not conn.autocommit:
        conn.commit()

    merged = int(agg["merged"])
    abandoned = int(agg["abandoned"])
    vp = int(agg["verify_passed"])
    vf = int(agg["verify_failed"])
    rekicks = int(agg["rekicks"])
    terminal = merged + abandoned
    n_completed = int(comp["n_completed"])

    return {
        "workstream": workstream,
        "totals": {
            "tasks_merged": merged,
            "tasks_abandoned": abandoned,
            "tasks_terminal": terminal,
            "verify_passed": vp,
            "verify_failed": vf,
            "rekicks": rekicks,
            "model_calls": int(agg["model_calls"]),
            "total_cost_usd": round(float(agg["cost_usd"]), 6),
            "total_tokens": int(agg["tokens"]),
        },
        "rates": {
            "task_success_rate": _ratio(merged, terminal),
            "verify_pass_rate": _ratio(vp, vp + vf),
            "rekick_rate": _ratio(rekicks, terminal),
            "error_rate": _ratio(abandoned + vf, terminal + vp + vf),
        },
        "cost": {
            "completed_tasks": n_completed,
            "avg_cost_per_completed_task_usd": _ratio(
                float(comp["completed_cost_usd"]), n_completed
            ),
            "avg_tokens_per_completed_task": _ratio(
                int(comp["completed_tokens"]), n_completed
            ),
        },
        "latency": {
            "avg_transition_latency_ms": int(agg["avg_transition_latency_ms"]),
            "avg_latency_per_completed_task_ms": _ratio(
                int(comp["completed_latency_ms"]), n_completed
            ),
        },
        # Reused existing rollup (process-wide, NOT workstream-filtered → _global).
        "by_model_global": model_rollup(conn),
        # Outcome-attribution: PM decision quality (trajectory → tasks it created →
        # their lifecycle outcomes), every rate carrying n + Wilson 95% CI + flag.
        "pm_decision_quality": pm_decision_quality(conn, workstream),
    }
