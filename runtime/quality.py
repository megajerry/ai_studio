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

NOTE (honest scope): dry-run means COST/TOKENS are the router's deterministic
dry-run estimates and OUTCOME quality is not yet measured — these rollups measure
MECHANISM + ops health now, and the same functions report real spend/quality once
real models are wired at go-live. See ``docs/evaluation.md``.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg

from .tasks import model_rollup


def _ratio(num: float, den: float) -> Optional[float]:
    """``num/den`` rounded, or ``None`` when the denominator is 0 (undefined)."""
    return round(num / den, 4) if den else None


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
    }
