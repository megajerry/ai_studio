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
- **grounding_global** — GROUNDING/FABRICATION telemetry from the S1 accountability
  ledger (``comms_claims`` + ``identity_trust``, ADR-0021): claim verification
  breakdown with a **verified-rate** and **fabrication-rate** (each via ``_rate_ci``
  → n + Wilson CI + flag), plus revoked/quarantined identity counts and total
  strikes — "is the grounding doctrine measurably working?". See
  :func:`grounding_report`.
- **capacity_global** — CAPACITY telemetry over the merged budget engine
  (``runtime.budget``, ADR-0022): for every budgeted ``(workstream, period)`` its
  current ``zone`` (ok→warn→throttle→reserve→over), spent-vs-cap, reserve headroom,
  recent burn rate, and a burn-projected exhaustion (``projected_breach``), plus a
  studio-wide roll-up (counts of allocations in each zone, an ``at_risk_rate`` via
  ``_rate_ci`` → n + Wilson CI + flag, and org-ceiling utilization). Read-only and
  None-safe on no budgets. See :func:`capacity_report`.

NOTE (honest scope): dry-run means COST/TOKENS are the router's deterministic
dry-run estimates and OUTCOME quality is not yet measured — these rollups measure
MECHANISM + ops health now, and the same functions report real spend/quality once
real models are wired at go-live. See ``docs/evaluation.md``.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import psycopg

from .budget import (
    DEFAULT_BURN_WINDOW_MIN,
    ORG_WORKSTREAM,
    ZONE_OK,
    ZONE_OVER,
    ZONE_RESERVE,
    ZONE_THROTTLE,
    ZONE_WARN,
)
from .budget import list_budgets as _list_budgets
from .budget import project_exhaustion as _project_exhaustion
from .budget import status as _budget_status
from .tasks import model_rollup

#: Capacity zones in escalation order — the roll-up counts every budgeted
#: ``(workstream, period)`` allocation that currently sits in each.
CAPACITY_ZONES = (ZONE_OK, ZONE_WARN, ZONE_THROTTLE, ZONE_RESERVE, ZONE_OVER)

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


def grounding_report(
    conn: psycopg.Connection, identity_prefix: Optional[str] = None
) -> dict:
    """Grounding/fabrication telemetry: is the comms doctrine measurably working?

    Rolls up the S1 accountability ledger (``comms_claims`` + ``identity_trust``,
    ADR-0021) into the numbers the Retro / Reviewer / a Grafana panel read to judge
    whether "everything said to the human is grounded" is actually holding. Read-only
    and derived entirely from those two tables (the append-only ``comms.*`` /
    ``trust.*`` events are the wire trail; the durable counts live in the tables).

    Comms/trust are **identity-scoped, not workstream-scoped** (a claim concerns an
    *identity*, not a vertical — ``comms_claims`` has no workstream column and the
    events log under the Spokesman's own workstream), so this rollup is GLOBAL by
    default. ``identity_prefix`` optionally restricts both tables to identities
    ``LIKE '<prefix>%'`` — used by the eval/tests to assert an exact known shape on a
    throwaway ``eval-grounding-*`` namespace without other rows polluting the counts.

    Returns (every rate via :func:`_rate_ci`, so it carries ``n`` + Wilson 95% CI +
    ``insufficient_sample``; None-safe on an empty ledger):

    - ``verification`` — ``verified`` / ``rejected`` / ``unverifiable`` / ``pending``
      counts, plus ``verified_rate`` and ``fabrication_rate`` (rejected / checked),
      where ``checked`` = verified + rejected + unverifiable (claims that got a
      verdict; ``pending``/NULL claims are excluded from the rate denominator).
    - ``counts`` — total claims, distinct originating identities, identities tracked
      in the trust ledger, and (from ``identity_trust``) ``revoked`` / ``quarantined``
      counts + ``total_strikes``.
    - ``top_offenders`` — identities with ≥1 rejected (fabricated) claim, most first.

    HONEST SCOPE: this measures the *ledger/telemetry* mechanism — that fabrications,
    once recorded, are counted and rated correctly. Whether the Spokesman gate + real
    models actually CATCH fabrication end-to-end is measured once those land (S2 gate).
    """
    pfx = f"{identity_prefix}%" if identity_prefix else None
    params = {"pfx": pfx}
    with conn.cursor() as cur:
        # --- claim verification breakdown (comms_claims) ----------------------
        cur.execute(
            """
            SELECT
              count(*)                                              AS total,
              count(DISTINCT originating_identity)                  AS identities,
              count(*) FILTER (WHERE verification_status='verified')     AS verified,
              count(*) FILTER (WHERE verification_status='rejected')     AS rejected,
              count(*) FILTER (WHERE verification_status='unverifiable') AS unverifiable,
              count(*) FILTER (WHERE verification_status IS NULL)        AS pending
            FROM comms_claims
            WHERE (%(pfx)s::text IS NULL OR originating_identity LIKE %(pfx)s)
            """,
            params,
        )
        c = cur.fetchone()

        # --- trust ledger (identity_trust) ------------------------------------
        cur.execute(
            """
            SELECT
              count(*)                                          AS identities_tracked,
              count(*) FILTER (WHERE trust_state='revoked')     AS revoked,
              count(*) FILTER (WHERE trust_state='quarantined') AS quarantined,
              COALESCE(sum(strikes), 0)                         AS total_strikes
            FROM identity_trust
            WHERE (%(pfx)s::text IS NULL OR identity LIKE %(pfx)s)
            """,
            params,
        )
        tr = cur.fetchone()

        # --- top offenders: identities with rejected (fabricated) claims ------
        cur.execute(
            """
            SELECT originating_identity AS identity,
                   count(*) FILTER (WHERE verification_status='rejected') AS fabrications
            FROM comms_claims
            WHERE (%(pfx)s::text IS NULL OR originating_identity LIKE %(pfx)s)
            GROUP BY originating_identity
            HAVING count(*) FILTER (WHERE verification_status='rejected') > 0
            ORDER BY fabrications DESC, identity
            LIMIT 10
            """,
            params,
        )
        offenders = cur.fetchall()
    if not conn.autocommit:
        conn.commit()

    verified = int(c["verified"])
    rejected = int(c["rejected"])
    unverifiable = int(c["unverifiable"])
    checked = verified + rejected + unverifiable

    return {
        "scope": "global" if identity_prefix is None else f"identity_prefix={identity_prefix!r}",
        "verification": {
            "verified": verified,
            "rejected": rejected,
            "unverifiable": unverifiable,
            "pending": int(c["pending"]),
            "checked": checked,
            # verified/rejected over CHECKED claims — each carries n + Wilson CI + flag.
            "verified_rate": _rate_ci(verified, checked),
            "fabrication_rate": _rate_ci(rejected, checked),
        },
        "counts": {
            "total_claims": int(c["total"]),
            "distinct_identities": int(c["identities"]),
            "identities_tracked": int(tr["identities_tracked"]),
            "revoked_identities": int(tr["revoked"]),
            "quarantined_identities": int(tr["quarantined"]),
            "total_strikes": int(tr["total_strikes"]),
        },
        "top_offenders": [
            {"identity": o["identity"], "fabrications": int(o["fabrications"])}
            for o in offenders
        ],
    }


def _utilization(spent: float, cap: Optional[float]) -> Optional[float]:
    """``spent/cap`` (a continuous utilization ratio, NOT a binomial proportion → no
    Wilson CI), or ``None`` when uncapped / a zero cap (undefined)."""
    if cap is None or cap <= 0:
        return None
    return round(spent / cap, 6)


def _capacity_entry(
    conn: psycopg.Connection, budget: Any, *, window_min: float
) -> dict:
    """Current capacity telemetry for ONE ``(workstream, period)`` allocation.

    Reads the merged budget engine (:func:`runtime.budget.status` /
    :func:`~runtime.budget.project_exhaustion`) at the current accrued spend (no
    pending estimate, so this is the ledger's *current* zone). ``projected_breach``
    is True when the recent burn rate projects the cap being reached — a finite
    positive ``calls_to_exhaustion`` — or the allocation is already ``over``. Numbers
    only (leak-free)."""
    st = _budget_status(conn, budget)  # est=0 → current standing, not a pending call
    frac = st.fraction()
    proj = _project_exhaustion(conn, budget.workstream, period=budget.period,
                               window_min=window_min)
    minutes_to_exh = None if proj is None else proj.minutes_to_exhaustion
    calls_to_exh = None if proj is None else proj.calls_to_exhaustion
    # Projected breach: at the observed burn the cap is finite-time reachable
    # (positive calls-to-exhaustion), or we are already over the hard cap. A
    # workstream with no recent burn projects None → not a breach.
    projected_breach = st.zone == ZONE_OVER or (
        calls_to_exh is not None and calls_to_exh > 0
    )
    burn = None if proj is None else {
        "window_min": proj.burn.window_min,
        "span_min": proj.burn.span_min,
        "calls": proj.burn.calls,
        "usd": round(proj.burn.usd, 6),
        "tokens": proj.burn.tokens,
        "usd_per_min": round(proj.burn.usd_per_min, 6),
        "tokens_per_min": round(proj.burn.tokens_per_min, 6),
        "usd_per_call": round(proj.burn.usd_per_call, 6),
        "tokens_per_call": round(proj.burn.tokens_per_call, 6),
    }
    return {
        "workstream": st.workstream,
        "period": st.period,
        "zone": st.zone,
        "cap_usd": st.cap_usd,
        "cap_tokens": st.cap_tokens,
        "spent_usd": round(st.spent_usd, 6),
        "spent_tokens": st.spent_tokens,
        "spent_frac": None if frac is None else round(frac, 6),
        "remaining_usd": (None if st.remaining_usd is None
                          else round(st.remaining_usd, 6)),
        "remaining_tokens": st.remaining_tokens,
        "reserve_headroom_usd": (None if st.reserve_headroom_usd is None
                                 else round(st.reserve_headroom_usd, 6)),
        "reserve_headroom_tokens": st.reserve_headroom_tokens,
        "burn": burn,
        "projection": {
            "minutes_to_exhaustion": minutes_to_exh,
            "calls_to_exhaustion": calls_to_exh,
            "projected_breach": projected_breach,
        },
    }


def capacity_report(
    conn: psycopg.Connection,
    *,
    workstream_prefix: Optional[str] = None,
    window_min: float = DEFAULT_BURN_WINDOW_MIN,
) -> dict:
    """Capacity telemetry over the merged budget engine (ADR-0022). Read-only.

    For every budgeted ``(workstream, period)`` allocation that has a cap (tracking-
    only rows with no cap are skipped — nothing to gate/project) it reports the
    current :attr:`~runtime.budget.BudgetStatus.zone`, spent-vs-cap, spent fraction,
    remaining + reserve headroom, the recent :func:`~runtime.budget.burn_rate`, and a
    burn-projected exhaustion (:func:`~runtime.budget.project_exhaustion`) with a
    ``projected_breach`` flag. The :data:`~runtime.budget.ORG_WORKSTREAM` sentinel is
    reported separately as the studio-wide ``org_ceiling`` (its spend is the org-wide
    total), NOT as a workstream.

    The ``rollup`` gives counts of allocations in each zone, the number projected to
    breach, and ``at_risk_rate`` — the fraction of allocations NOT in the ``ok`` zone.
    That IS a binomial proportion over ``n`` = budgeted allocations, so it carries n +
    Wilson 95% CI + ``insufficient_sample`` via :func:`_rate_ci` (unlike the spent /
    utilization *ratios*, which are continuous and reported bare, mirroring
    ``rekick_rate``). None-safe: no budgets → empty lists, zero counts, ``at_risk_rate``
    over n=0 (rate ``None``), and ``org_ceiling`` ``None``.

    ``workstream_prefix`` optionally restricts the budgeted workstreams to those
    ``LIKE '<prefix>%'`` (mirroring :func:`grounding_report`'s ``identity_prefix``) —
    used by the eval/tests to assert an exact known shape on a throwaway
    ``eval-capacity-*`` namespace. When a prefix is set the global ``org_ceiling``
    sentinel is omitted (``None``), since it is not prefix-scopable.
    """
    # Distinct budgeted workstreams (the org sentinel is handled separately).
    pfx = f"{workstream_prefix}%" if workstream_prefix else None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT workstream FROM budgets "
            "WHERE workstream <> %(org)s "
            "  AND (%(pfx)s::text IS NULL OR workstream LIKE %(pfx)s) "
            "ORDER BY workstream",
            {"org": ORG_WORKSTREAM, "pfx": pfx},
        )
        workstreams = [r["workstream"] for r in cur.fetchall()]
    if not conn.autocommit:
        conn.commit()

    entries: list[dict] = []
    for ws in workstreams:
        for b in _list_budgets(conn, ws):
            if b.cap_usd is None and b.cap_tokens is None:
                continue  # tracking-only row: no ceiling to gate/project against
            entries.append(_capacity_entry(conn, b, window_min=window_min))

    zone_counts = {z: 0 for z in CAPACITY_ZONES}
    projected_breaches = 0
    for e in entries:
        zone_counts[e["zone"]] = zone_counts.get(e["zone"], 0) + 1
        if e["projection"]["projected_breach"]:
            projected_breaches += 1
    n = len(entries)
    at_risk = n - zone_counts[ZONE_OK]

    # Org/key ceiling (the __org__ sentinel): utilization = org-wide spend / cap.
    # It is the studio-wide sentinel, not prefix-scopable, so it is omitted when a
    # workstream_prefix restricts the report to a throwaway namespace.
    org_ceiling: list[dict] = []
    for b in ([] if workstream_prefix else _list_budgets(conn, ORG_WORKSTREAM)):
        if b.cap_usd is None and b.cap_tokens is None:
            continue
        st = _budget_status(conn, b)  # for ORG this sums org-wide spend
        org_ceiling.append({
            "period": st.period,
            "zone": st.zone,
            "cap_usd": st.cap_usd,
            "cap_tokens": st.cap_tokens,
            "spent_usd": round(st.spent_usd, 6),
            "spent_tokens": st.spent_tokens,
            "utilization_usd": _utilization(st.spent_usd, st.cap_usd),
            "utilization_tokens": _utilization(float(st.spent_tokens), st.cap_tokens),
        })

    return {
        "workstreams_budgeted": len(workstreams),
        "allocations_scored": n,
        "by_workstream": entries,
        "org_ceiling": org_ceiling or None,
        "rollup": {
            "zone_counts": zone_counts,
            "projected_breaches": projected_breaches,
            # Fraction of budgeted allocations NOT in the healthy 'ok' zone — a
            # proper proportion over n allocations → n + Wilson CI + flag.
            "at_risk_rate": _rate_ci(at_risk, n),
        },
    }


def failure_report(
    conn: psycopg.Connection,
    workstream: Optional[str] = None,
    *,
    since_seq: Optional[int] = None,
) -> dict:
    """Failure telemetry rollup: WHAT is breaking, HOW OFTEN, and HOW SURE (ADR-0023).

    Rolls the append-only event log up into the failure signal the R3 failure-pattern
    detector reads to recognize a RECURRING failure and the fix-experiment reads to
    judge a proposed fix on real traffic. Everything is derived from the immutable
    events (``model.call.failed`` / ``task.rekicked`` / ``task.stuck`` /
    ``task.transition`` / ``verify.*``) — no new capture, so it is replayable.

    ``workstream=None`` spans ALL workstreams. ``since_seq`` (exclusive on the
    monotonic ``events.seq`` cursor — the same idiom as
    :func:`runtime.events.read_events`) scopes the rollup to events *after* a cursor,
    so a fix experiment can measure only POST-FIX traffic (capture the cursor when the
    fix is applied, then read the rate over what happened since). ``ts`` is NOT used
    for the window because it is the transaction-start time (see migration 0004).

    Returns (None-safe — every proportion is ``None`` on a zero denominator, never a
    divide-by-zero, and each carries ``n`` + Wilson 95% CI + ``insufficient_sample``
    via :func:`_rate_ci`):

    - ``totals`` — raw counts (model calls ok/failed/total, rekicks, stucks, terminal
      tasks, verify passed/failed).
    - ``rates`` — ``model_call_error_rate`` (failed / all model calls),
      ``rekick_rate`` (rekicks / terminal), and ``error_rate`` (matching
      :func:`quality_report`: (abandoned + verify.failed) / all outcome signals).
    - ``by_error_type`` — ``model.call.failed`` broken out by the body-free
      ``error_type`` CLASS, each with its ``count`` and ``share`` = that class as a
      proportion of ALL model calls (``_rate_ci`` over ``n`` = total calls) — the
      per-class recurrence signal the detector fires on.
    - ``by_stall_reason`` — ``task.stuck`` broken out by the ``stall_reason`` CODE,
      each with its ``count`` and ``share`` = that reason as a proportion of terminal
      tasks (``_rate_ci`` over ``n`` = terminal tasks).
    """
    params = {"ws": workstream, "since_seq": since_seq}
    _window = "AND (%(since_seq)s::bigint IS NULL OR seq > %(since_seq)s::bigint)"
    _ws = "(%(ws)s::text IS NULL OR workstream = %(ws)s::text)"
    with conn.cursor() as cur:
        # --- aggregate counts ------------------------------------------------
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE type='model.call')            AS model_calls_ok,
              count(*) FILTER (WHERE type='model.call.failed')     AS model_calls_failed,
              count(*) FILTER (WHERE type='task.rekicked')         AS rekicks,
              count(*) FILTER (WHERE type='task.stuck')            AS stucks,
              count(*) FILTER (WHERE type='task.transition'
                               AND payload->>'to'='merged')        AS merged,
              count(*) FILTER (WHERE type='task.transition'
                               AND payload->>'to'='abandoned')     AS abandoned,
              count(*) FILTER (WHERE type='verify.passed')         AS verify_passed,
              count(*) FILTER (WHERE type='verify.failed')         AS verify_failed
            FROM events
            WHERE {_ws} {_window}
            """,
            params,
        )
        agg = cur.fetchone()

        # --- model.call.failed by error_type CLASS ---------------------------
        cur.execute(
            f"""
            SELECT payload->>'error_type' AS error_type, count(*) AS n
            FROM events
            WHERE type='model.call.failed' AND {_ws} {_window}
            GROUP BY payload->>'error_type'
            ORDER BY n DESC, error_type
            """,
            params,
        )
        error_rows = cur.fetchall()

        # --- task.stuck by stall_reason CODE ---------------------------------
        cur.execute(
            f"""
            SELECT payload->>'stall_reason' AS stall_reason, count(*) AS n
            FROM events
            WHERE type='task.stuck' AND {_ws} {_window}
            GROUP BY payload->>'stall_reason'
            ORDER BY n DESC, stall_reason
            """,
            params,
        )
        stall_rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()

    ok = int(agg["model_calls_ok"])
    failed = int(agg["model_calls_failed"])
    total_calls = ok + failed
    rekicks = int(agg["rekicks"])
    merged = int(agg["merged"])
    abandoned = int(agg["abandoned"])
    vp = int(agg["verify_passed"])
    vf = int(agg["verify_failed"])
    terminal = merged + abandoned

    return {
        "workstream": workstream,
        "window": {"since_seq": since_seq},
        "totals": {
            "model_calls_ok": ok,
            "model_calls_failed": failed,
            "model_calls_total": total_calls,
            "rekicks": rekicks,
            "stucks": int(agg["stucks"]),
            "tasks_terminal": terminal,
            "verify_passed": vp,
            "verify_failed": vf,
        },
        "rates": {
            # Fraction of ALL model calls that died with a provider error — the
            # headline API-error failure signal (n = total model calls).
            "model_call_error_rate": _rate_ci(failed, total_calls),
            "rekick_rate": _rate_ci(rekicks, terminal),
            "error_rate": _rate_ci(abandoned + vf, terminal + vp + vf),
        },
        # Each error_type CLASS as a share of ALL model calls (n = total calls).
        "by_error_type": [
            {
                "error_type": r["error_type"],
                "count": int(r["n"]),
                "share": _rate_ci(int(r["n"]), total_calls),
            }
            for r in error_rows
        ],
        # Each stall_reason CODE as a share of terminal tasks (n = terminal).
        "by_stall_reason": [
            {
                "stall_reason": r["stall_reason"],
                "count": int(r["n"]),
                "share": _rate_ci(int(r["n"]), terminal),
            }
            for r in stall_rows
        ],
    }


def quality_report(
    conn: psycopg.Connection, workstream: Optional[str] = None
) -> dict:
    """Compute the workstream quality/ops rollup from the live telemetry.

    ``workstream=None`` reports across ALL workstreams. Returns a nested dict of
    ``totals`` (raw counts), ``rates`` (the derived quality ratios), ``cost`` and
    ``latency`` (per-completed-task efficiency), ``by_model_global`` (the reused
    :func:`runtime.tasks.model_rollup`, which is process-wide, not ws-filtered — so
    it is labeled ``_global``), ``grounding_global`` (the identity-scoped
    :func:`grounding_report` — also global, not ws-filtered), and
    ``capacity_global`` (the :func:`capacity_report` over the budget engine — spans
    all budgeted workstreams, so also global). Reads only the append-only event log +
    ``task_transitions`` (+ the S1 comms/trust ledger + the ``budgets`` table).
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
        # Grounding/fabrication telemetry from the S1 accountability ledger. Comms/
        # trust are identity-scoped (not workstream-scoped), so this is GLOBAL like
        # by_model_global — hence the _global suffix. Rates carry n + Wilson CI + flag.
        "grounding_global": grounding_report(conn),
        # Capacity telemetry over the merged budget engine (ADR-0022): per-budgeted-
        # workstream zone/spend/headroom/burn/projection + a studio-wide roll-up.
        # Spans ALL budgeted workstreams (not ws-filtered) → _global, None-safe.
        "capacity_global": capacity_report(conn),
        # Failure telemetry (ADR-0023 R3): model.call.failed by error_type + task.stuck
        # by stall_reason + failure rates, each proportion carrying n + Wilson CI + flag.
        # Workstream-scoped like the counts above; None-safe on empty.
        "failure": failure_report(conn, workstream),
    }
