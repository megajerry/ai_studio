"""Capacity Steward — the OPTIONAL, config-driven capacity monitor (ADR-0022 C2).

The studio is budget-bounded (docs/cost-model.md §8): every workstream runs under a
ceiling and the deterministic engine (:mod:`runtime.budget`, C1) meters + gates each
model call by zone (``ok → warn → throttle → reserve → over``). That engine is the
ENFORCEMENT half — it is the ONLY thing that blocks a call, and raising a ceiling is
a 🛑 PM/stakeholder decision (ADR-0006).

The Capacity Steward is the BEHAVIORAL half on top of it. It reads the SAME live
facts the engine reads — a workstream's remaining headroom (:func:`runtime.budget.remaining`),
its recent burn (:func:`runtime.budget.burn_rate`), and its projected exhaustion
(:func:`runtime.budget.project_exhaustion`) — and, when a workstream is heading for
trouble, it **FLAGS early** and **RECOMMENDS an action** as reviewable output +
events. It changes nothing itself:

- it NEVER enforces (the engine does — no ``budget.enforce`` call here);
- it NEVER raises a ceiling (that stays a 🛑 PM/stakeholder decision, ADR-0006 — the
  steward can only RECOMMEND ``escalate``, which a human then decides);
- it emits only BODY-FREE ``capacity.flagged`` / ``capacity.recommendation`` events
  (ids / workstream / period / zone / amounts / a recommended-action enum — never a
  prompt, arg, or secret; invariants 5 & 6).

**Accountability (stakeholder decision).** The **PM is the accountable steward by
default** — every role is already budget-aware (see
:func:`runtime.roles.prompt.compose_role_prompt`). This dedicated Capacity Steward
role is **OPTIONAL and OFF by default**, enabled per-workstream via config
(``capacity_steward.enabled: true`` in ``workstreams/<name>/config.yaml``) once a
vertical is at enough scale to want a dedicated monitor. A workstream without that
config never activates it — config-not-code (ADR-0002/0018), no code change to turn
it on or off.

The verdict is pure + deterministic (computed from the numeric budget facts), so it
is reproducible and testable keyless. Like the Critic it acts only through the
sanctioned seams and coordinates via events, never by calling another agent
(CLAUDE.md invariant 1).
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from ..budget import (
    DEFAULT_BURN_WINDOW_MIN,
    ORG_WORKSTREAM,
    ZONE_OK,
    ZONE_OVER,
    ZONE_RESERVE,
    ZONE_THROTTLE,
    ZONE_WARN,
    Budget,
    list_budgets,
    project_exhaustion,
    remaining,
)
from ..enforce import EventSink, NullEventSink
from ..event_types import EVENT_CAPACITY_FLAGGED, EVENT_CAPACITY_RECOMMENDATION
from ..models import make_event

log = logging.getLogger("runtime.roles.capacity_steward")

#: The role name (for events / traceability). PM is the DEFAULT steward; this is the
#: optional dedicated one.
ROLE = "capacity_steward"

#: Recommended actions the steward can surface — a closed, leak-free vocabulary
#: (reviewable output, never enforcement). All map to something a role/PM can DO
#: BEFORE the engine has to block a call:
#: - ``compact``    — compact context + checkpoint (ADR-0013); approaching the cap.
#: - ``reallocate`` — shift/limit work (fewer parallel tasks, cheaper models); burning hot.
#: - ``pivot``      — re-plan scope: the current pace won't finish inside the period.
#: - ``escalate``   — wind down and, if truly needed, escalate for more budget (🛑 PM/
#:                    stakeholder decision, ADR-0006) BEFORE breaching. Never self-raised.
ACTION_COMPACT = "compact"
ACTION_REALLOCATE = "reallocate"
ACTION_PIVOT = "pivot"
ACTION_ESCALATE = "escalate"
ACTIONS = frozenset({ACTION_COMPACT, ACTION_REALLOCATE, ACTION_PIVOT, ACTION_ESCALATE})


class CapacityFlag(BaseModel):
    """One flagged ``(workstream, period)`` cap + the steward's recommended action.

    Carries only NUMBERS / enums (never a prompt/arg/secret), so it is safe to log,
    surface, and serialize onto a body-free event.
    """

    workstream: str
    period: str
    zone: str
    action: str
    #: True when the recent burn projects exhaustion BEFORE the period ends.
    projected_breach: bool
    spent_usd: float = 0.0
    spent_tokens: int = 0
    cap_usd: Optional[float] = None
    cap_tokens: Optional[int] = None
    remaining_usd: Optional[float] = None
    remaining_tokens: Optional[int] = None
    minutes_to_exhaustion: Optional[float] = None
    calls_to_exhaustion: Optional[float] = None
    #: Minutes until the period boundary (``None`` for open-ended windows).
    horizon_minutes: Optional[float] = None

    def flag_payload(self) -> dict:
        """Body-free payload for a ``capacity.flagged`` event (numbers/enums only)."""
        return {
            "workstream": self.workstream,
            "period": self.period,
            "zone": self.zone,
            "projected_breach": self.projected_breach,
            "spent_usd": round(self.spent_usd, 6),
            "spent_tokens": self.spent_tokens,
            "cap_usd": self.cap_usd,
            "cap_tokens": self.cap_tokens,
            "remaining_usd": (
                None if self.remaining_usd is None else round(self.remaining_usd, 6)
            ),
            "remaining_tokens": self.remaining_tokens,
            "minutes_to_exhaustion": self.minutes_to_exhaustion,
            "calls_to_exhaustion": self.calls_to_exhaustion,
            "horizon_minutes": (
                None if self.horizon_minutes is None else round(self.horizon_minutes, 4)
            ),
        }

    def recommendation_payload(self) -> dict:
        """Body-free payload for a ``capacity.recommendation`` event."""
        return {
            "workstream": self.workstream,
            "period": self.period,
            "zone": self.zone,
            "action": self.action,
            "projected_breach": self.projected_breach,
            "minutes_to_exhaustion": self.minutes_to_exhaustion,
            "horizon_minutes": (
                None if self.horizon_minutes is None else round(self.horizon_minutes, 4)
            ),
        }


class CapacityReport(BaseModel):
    """What one steward pass observed — the flags it raised (for the caller/tests)."""

    workstreams_checked: int = 0
    flags: list[CapacityFlag] = Field(default_factory=list)

    @property
    def flagged_count(self) -> int:
        return len(self.flags)


def recommend_action(zone: str, projected_breach: bool) -> str:
    """Map a zone (+ whether burn projects a pre-period-end breach) → an action.

    Pure + deterministic. Reserve/over → ``escalate`` (wind down / ask a human for
    more budget BEFORE breaching); throttle → ``reallocate``; warn → ``compact``; an
    ``ok`` zone that is nonetheless projected to breach before the period ends →
    ``pivot`` (re-plan scope). This is only a RECOMMENDATION — nothing here enforces.
    """
    if zone in (ZONE_OVER, ZONE_RESERVE):
        return ACTION_ESCALATE
    if zone == ZONE_THROTTLE:
        return ACTION_REALLOCATE
    if zone == ZONE_WARN:
        return ACTION_COMPACT
    return ACTION_PIVOT  # ok zone but projected to breach before period end


def _period_end_minutes(conn: Any, period: str) -> Optional[float]:
    """Minutes from now until the end of ``period``'s window, or ``None`` if open-ended.

    Computed against the DB's ``now()`` (the same clock ``model.call`` events and the
    budget windows use) so the horizon lines up with accrued spend. ``rolling_30d`` /
    ``all_time`` have no fixed boundary → ``None``. The SQL expression is chosen from
    a constant per branch (never interpolated caller input), so it is injection-safe.
    """
    if period == "daily":
        expr = "date_trunc('day', now()) + interval '1 day' - now()"
    elif period == "monthly":
        expr = "date_trunc('month', now()) + interval '1 month' - now()"
    else:
        return None  # rolling_30d / all_time: no fixed period boundary
    with conn.cursor() as cur:
        cur.execute(f"SELECT EXTRACT(EPOCH FROM ({expr})) / 60.0 AS m")
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return None if row is None or row["m"] is None else float(row["m"])


def assess_budget(
    conn: Any,
    budget: Budget,
    *,
    window_min: float = DEFAULT_BURN_WINDOW_MIN,
    horizon_min: Optional[float] = None,
) -> Optional[CapacityFlag]:
    """Assess ONE ``(workstream, period)`` cap → a :class:`CapacityFlag`, or ``None``.

    Returns ``None`` (nothing to flag) when the workstream is uncapped for the period,
    or when it is comfortably in the ``ok`` zone AND not projected to breach before the
    period ends. Otherwise it flags — computing the recommended action deterministically
    from the current zone + projection. Reads the same live facts the engine reads;
    changes nothing.
    """
    st = remaining(conn, budget.workstream, period=budget.period)
    if st is None:  # uncapped resource → nothing to steward
        return None
    zone = st.zone  # est=0 ⇒ current spent-fraction zone
    proj = project_exhaustion(
        conn, budget.workstream, period=budget.period, window_min=window_min
    )
    mte = proj.minutes_to_exhaustion if proj is not None else None
    cte = proj.calls_to_exhaustion if proj is not None else None
    horizon = horizon_min if horizon_min is not None else _period_end_minutes(conn, budget.period)
    projected_breach = mte is not None and horizon is not None and mte <= horizon

    # Quiet on the happy path: an ok zone with no pre-period-end breach is not flagged.
    if zone == ZONE_OK and not projected_breach:
        return None

    return CapacityFlag(
        workstream=budget.workstream,
        period=budget.period,
        zone=zone,
        action=recommend_action(zone, projected_breach),
        projected_breach=projected_breach,
        spent_usd=st.spent_usd,
        spent_tokens=st.spent_tokens,
        cap_usd=st.cap_usd,
        cap_tokens=st.cap_tokens,
        remaining_usd=st.remaining_usd,
        remaining_tokens=st.remaining_tokens,
        minutes_to_exhaustion=mte,
        calls_to_exhaustion=cte,
        horizon_minutes=horizon,
    )


def _budgeted_workstreams(conn: Any) -> list[str]:
    """All workstreams that have at least one budget row (excluding the org sentinel)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT workstream FROM budgets WHERE workstream <> %s ORDER BY workstream",
            (ORG_WORKSTREAM,),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [r["workstream"] for r in rows]


def run_capacity_steward(
    conn: Any,
    sink: Optional[EventSink] = None,
    *,
    workstream: Optional[str] = None,
    window_min: float = DEFAULT_BURN_WINDOW_MIN,
    horizon_min: Optional[float] = None,
    task_id: Optional[UUID] = None,
) -> CapacityReport:
    """Monitor budget burn and FLAG + RECOMMEND early — the steward's one pass.

    Assesses every enforceable ``(workstream, period)`` cap (a single ``workstream``
    when given, else ALL budgeted workstreams) against its live remaining headroom +
    recent burn + projected exhaustion. For each cap heading for trouble — in a hot
    zone (warn/throttle/reserve/over) OR projected to exhaust before the period ends —
    it emits a body-free ``capacity.flagged`` and a ``capacity.recommendation``
    (compact / reallocate / pivot / escalate) and returns the flag.

    It NEVER enforces (no ``budget.enforce`` call) and NEVER raises a ceiling (an
    ``escalate`` recommendation is for a human to act on — 🛑 ADR-0006). All output is
    reviewable telemetry. Off by default at the studio level — a caller decides whether
    to run it (e.g. gated by :func:`capacity_steward_enabled` on a workstream config).
    """
    sink = sink or NullEventSink()
    if workstream is None:
        names = _budgeted_workstreams(conn)
        budgets = [b for name in names for b in list_budgets(conn, name)]
    else:
        budgets = list_budgets(conn, workstream)
    # Tracking-only rows (no cap set) constrain nothing → skip.
    budgets = [b for b in budgets if not (b.cap_usd is None and b.cap_tokens is None)]

    checked = len({b.workstream for b in budgets})
    flags: list[CapacityFlag] = []
    for b in budgets:
        flag = assess_budget(conn, b, window_min=window_min, horizon_min=horizon_min)
        if flag is None:
            continue
        flags.append(flag)
        sink.emit(
            make_event(
                workstream=b.workstream,
                type=EVENT_CAPACITY_FLAGGED,
                task_id=task_id,
                payload=flag.flag_payload(),
            )
        )
        sink.emit(
            make_event(
                workstream=b.workstream,
                type=EVENT_CAPACITY_RECOMMENDATION,
                task_id=task_id,
                payload=flag.recommendation_payload(),
            )
        )
    return CapacityReport(workstreams_checked=checked, flags=flags)


def capacity_steward_enabled(cfg: Any) -> bool:
    """Whether a workstream's config opts INTO the dedicated Capacity Steward.

    OFF by default: a ``None`` config (workstream has no config file) or a config
    without a ``capacity_steward`` block returns ``False`` (PM stays the accountable
    steward). A config with ``capacity_steward.enabled: true`` returns ``True`` —
    config-not-code, no code change to turn the role on for a vertical.
    """
    spec = getattr(cfg, "capacity_steward", None) if cfg is not None else None
    return bool(spec is not None and getattr(spec, "enabled", False))
