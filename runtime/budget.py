"""Per-workstream budget enforcement — real caps that actually gate (ADR-0006/0012).

The cost model (docs/cost-model.md §8) says the studio is **budget-bounded**: a
workstream runs under a ceiling, and *raising* that ceiling is a 🛑 stakeholder
decision (ADR-0006), never a silent overspend. This module is the enforcement
half of that:

- A ``budgets`` table (migration ``0010_budgets.sql``) holds, per
  ``(workstream, period)``, a ``cap_usd`` and/or ``cap_tokens`` ceiling. A period
  is a time window: ``daily`` / ``monthly`` / ``rolling_30d`` / ``all_time``.
- :func:`spent` sums a workstream's **real accrued** cost/tokens from the
  ``model.call`` events (the same source :func:`runtime.tasks.task_cost` /
  ``model_rollup`` read) within the period's window.
- :func:`remaining` / :func:`status` turn a cap + accrued spend into a
  :class:`BudgetStatus`, whose over-budget test is the SAME predicate the policy
  engine uses (:attr:`runtime.policy.BudgetContext.would_exceed`) — so budget
  enforcement and the policy engine can never disagree.
- :func:`enforce` is the gate the instrumented model-call path calls before a
  (real or dry-run) call. Graduated capacity governance (ADR-0022) makes it
  tiered rather than a hard binary cap: from the spent-fraction vs a row's
  configured thresholds it computes a **zone** (``ok → warn → throttle → reserve
  → over``) and gates accordingly — ``ok`` emits ``budget.checkpoint`` (as
  before); ``warn`` / ``throttle`` emit non-blocking telemetry and allow;
  **reserve** (the buffer near the cap) is spendable ONLY on a
  ``wind_down`` / ``escalation`` ``purpose`` so a workstream can react/pivot/
  escalate BEFORE breaching (a ``normal`` call is withheld, emitting
  ``budget.reserve``); **over** emits ``budget.exceeded``, raises the 🛑
  :func:`runtime.approvals.request_approval` ("raise budget for <workstream>"),
  and raises :class:`OverBudget` — the call does NOT proceed. The call is checked
  against BOTH the per-workstream allocation AND the org ceiling (the
  :data:`ORG_WORKSTREAM` sentinel row); the tighter one wins. A row with NO
  threshold fractions has only ``ok`` / ``over`` — the old hard cap, unchanged.
- :func:`burn_rate` / :func:`project_exhaustion` read the same ``model.call``
  source to project when a workstream will exhaust its cap, so it can be flagged
  EARLY. Leak-free (numbers only).

Events carry only amounts + the workstream/period — never prompts, args, or
secrets (CLAUDE.md invariants 5 & 6). All functions take an open ``conn`` (the
caller owns the transaction boundary, matching :mod:`runtime.tasks` /
:mod:`runtime.approvals`); a workstream with no ``budgets`` row is simply
unconstrained, so the whole studio is unaffected until a cap is set.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional
from uuid import UUID

import psycopg

from .approvals import request_approval as _request_approval
from .event_types import (
    EVENT_BUDGET_CHECKPOINT,
    EVENT_BUDGET_EXCEEDED,
    EVENT_BUDGET_RESERVE,
    EVENT_BUDGET_THROTTLE,
    EVENT_BUDGET_WARN,
)
from .models import make_event
from .policy import BudgetContext

if TYPE_CHECKING:  # avoid importing the enforce module at runtime (no cycle)
    from .enforce import EventSink

# The budget wire strings (``EVENT_BUDGET_EXCEEDED`` on a blocked over-cap call,
# ``EVENT_BUDGET_CHECKPOINT`` on an under-cap spend checkpoint, and the graduated
# ``budget.warn`` / ``budget.throttle`` / ``budget.reserve`` tier events) are
# imported from the canonical :mod:`runtime.event_types`.

#: The default window when a caller does not name one.
DEFAULT_PERIOD = "monthly"
#: Windows a budget may be scoped to. ``all_time`` = the workstream's whole history.
VALID_PERIODS = ("daily", "monthly", "rolling_30d", "all_time")

#: Tier shown on the 🛑 "raise budget" approval — ADR-0006 "approve (blocks)".
RAISE_BUDGET_TIER = "🛑"

# --- Graduated capacity governance (ADR-0022) -------------------------------
#: Reserved sentinel workstream holding the ORG / key-level ceiling. A per-
#: workstream call is checked against BOTH its own allocation AND this org row;
#: the org row's spend is summed across ALL workstreams (:func:`org_spent`).
ORG_WORKSTREAM = "__org__"

#: Capacity zones, from a computed spent-fraction vs the configured thresholds.
#: A row with NO threshold fractions has only ``ok`` (under cap) and ``over``
#: (would exceed) — the pre-ADR-0022 hard-cap behavior.
ZONE_OK = "ok"
ZONE_WARN = "warn"
ZONE_THROTTLE = "throttle"
ZONE_RESERVE = "reserve"
ZONE_OVER = "over"

#: Suggested threshold fractions when a caller opts into graduated governance.
#: The schema never back-fills these (NULL = hard-cap only, back-compatible).
DEFAULT_WARN_FRAC = 0.70
DEFAULT_THROTTLE_FRAC = 0.85
DEFAULT_RESERVE_FRAC = 0.90

#: Purpose of a gated call. The RESERVE buffer near the cap is spendable ONLY on
#: ``wind_down`` / ``escalation`` (react/pivot/escalate) — a ``normal`` call in
#: the reserve zone is withheld so the buffer is preserved.
PURPOSE_NORMAL = "normal"
PURPOSE_WIND_DOWN = "wind_down"
PURPOSE_ESCALATION = "escalation"
VALID_PURPOSES = (PURPOSE_NORMAL, PURPOSE_WIND_DOWN, PURPOSE_ESCALATION)
#: Purposes allowed to spend the reserve buffer (everything except ``normal``).
RESERVE_PURPOSES = (PURPOSE_WIND_DOWN, PURPOSE_ESCALATION)

#: Zone → the (non-blocking) event emitted when a call is ALLOWED in that zone.
_ZONE_EVENT = {
    ZONE_OK: EVENT_BUDGET_CHECKPOINT,
    ZONE_WARN: EVENT_BUDGET_WARN,
    ZONE_THROTTLE: EVENT_BUDGET_THROTTLE,
    ZONE_RESERVE: EVENT_BUDGET_RESERVE,
}

# Which model.call the estimate models — kept consistent with the dry-run
# provider so a keyless estimate matches the keyless actual cost.
_CHARS_PER_TOKEN = 4


class OverBudget(Exception):
    """Raised when a gated model call is BLOCKED by a workstream/org ceiling.

    Distinct from :class:`runtime.model.router.OverBudget` (which is about a
    *routing tier* having no cheaper fallback): this is the deterministic
    per-workstream / org capacity gate. It covers two block reasons, told apart
    by :attr:`status.zone`:

    - ``over`` — committing the call would cross the hard cap (allocation OR org
      ceiling): the pre-ADR-0022 behavior, paired with a 🛑 "raise budget" approval.
    - ``reserve`` — a ``normal`` call landed in the reserve buffer near the cap
      (ADR-0022): it is WITHHELD so the buffer is preserved for
      ``wind_down`` / ``escalation``; NO approval is raised.

    Carries only leak-free numbers so it is safe to log/surface.
    """

    def __init__(self, status: "BudgetStatus") -> None:
        self.status = status
        self.workstream = status.workstream
        self.period = status.period
        self.zone = status.zone
        super().__init__(
            f"budget blocked for workstream {status.workstream!r} "
            f"({status.period}) in {status.zone} zone: " + status.block_reason()
        )


# --- Row / value models -----------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """One configured cap for a ``(workstream, period)``.

    ``warn_frac`` / ``throttle_frac`` / ``reserve_frac`` are optional graduated
    thresholds (ADR-0022): a spent-fraction in ``(0, 1)`` marking the start of
    each zone. ALL-NULL fractions ⇒ the old hard-cap behavior (only ``ok`` and
    ``over``). When set they must nest: ``warn <= throttle <= reserve``.
    """

    workstream: str
    period: str
    cap_usd: Optional[float] = None
    cap_tokens: Optional[int] = None
    warn_frac: Optional[float] = None
    throttle_frac: Optional[float] = None
    reserve_frac: Optional[float] = None


@dataclass(frozen=True)
class Spend:
    """Real accrued spend for a workstream within a window (from ``model.call``)."""

    cost_usd: float = 0.0
    tokens: int = 0
    calls: int = 0


@dataclass(frozen=True)
class BudgetStatus:
    """A cap + its accrued spend + the pending call's estimate → an over/under verdict.

    :meth:`context` projects this onto the policy engine's
    :class:`~runtime.policy.BudgetContext`, and :attr:`would_exceed` /
    :meth:`exceed_reason` delegate to it, so the budget layer and the policy
    engine share ONE decision predicate.
    """

    workstream: str
    period: str
    cap_usd: Optional[float]
    cap_tokens: Optional[int]
    spent_usd: float
    spent_tokens: int
    est_usd: float = 0.0
    est_tokens: int = 0
    warn_frac: Optional[float] = None
    throttle_frac: Optional[float] = None
    reserve_frac: Optional[float] = None

    def context(self) -> BudgetContext:
        """The equivalent :class:`~runtime.policy.BudgetContext` (real spend)."""
        return BudgetContext(
            spent_tokens=self.spent_tokens,
            budget_tokens=self.cap_tokens,
            estimated_tokens=self.est_tokens,
            spent_usd=self.spent_usd,
            budget_usd=self.cap_usd,
            estimated_usd=self.est_usd,
        )

    @property
    def would_exceed(self) -> bool:
        """True if committing the estimated call would break a configured cap."""
        return self.context().would_exceed

    @property
    def remaining_usd(self) -> Optional[float]:
        return None if self.cap_usd is None else self.cap_usd - self.spent_usd

    @property
    def remaining_tokens(self) -> Optional[int]:
        return None if self.cap_tokens is None else self.cap_tokens - self.spent_tokens

    def exceed_reason(self) -> str:
        """Leak-free description of which cap would be broken (numbers only)."""
        return self.context().exceed_reason()

    # --- Graduated capacity governance (ADR-0022) ---------------------------

    def fraction(self) -> Optional[float]:
        """Projected spent-fraction ``(spent+est)/cap`` — the MAX across configured
        caps (the tightest resource), or ``None`` if no cap is set."""
        fracs: list[float] = []
        if self.cap_usd is not None and self.cap_usd > 0:
            fracs.append((self.spent_usd + self.est_usd) / self.cap_usd)
        if self.cap_tokens is not None and self.cap_tokens > 0:
            fracs.append((self.spent_tokens + self.est_tokens) / self.cap_tokens)
        return max(fracs) if fracs else None

    @property
    def zone(self) -> str:
        """Capacity zone for this projected call.

        ``over`` (would cross the hard cap) always wins. Otherwise the tightest
        configured threshold the fraction has reached decides: reserve → throttle
        → warn → ok. Thresholds that are ``None`` are skipped, so a cap-only row
        (no fractions) only ever reports ``ok`` or ``over`` — the old behavior.
        """
        if self.would_exceed:
            return ZONE_OVER
        frac = self.fraction()
        if frac is None:
            return ZONE_OK  # unconstrained resource
        if self.reserve_frac is not None and frac >= self.reserve_frac:
            return ZONE_RESERVE
        if self.throttle_frac is not None and frac >= self.throttle_frac:
            return ZONE_THROTTLE
        if self.warn_frac is not None and frac >= self.warn_frac:
            return ZONE_WARN
        return ZONE_OK

    @property
    def reserve_headroom_usd(self) -> Optional[float]:
        """USD still spendable before the hard cap — the buffer preserved for
        wind-down/escalation once in the reserve zone. ``None`` if no USD cap or
        no reserve threshold is configured."""
        if self.cap_usd is None or self.reserve_frac is None:
            return None
        return self.cap_usd - self.spent_usd

    @property
    def reserve_headroom_tokens(self) -> Optional[int]:
        """Tokens still spendable before the hard cap (see
        :attr:`reserve_headroom_usd`)."""
        if self.cap_tokens is None or self.reserve_frac is None:
            return None
        return self.cap_tokens - self.spent_tokens

    def block_reason(self) -> str:
        """Leak-free description of WHY a call is blocked (numbers only)."""
        if self.zone == ZONE_RESERVE:
            frac = self.fraction()
            return (
                f"reserve zone: fraction {frac:.4f} >= reserve {self.reserve_frac} "
                "(buffer preserved for wind_down/escalation)"
            )
        return self.exceed_reason()

    def to_payload(self) -> dict:
        """JSON-serializable, leak-free summary for a budget.* event."""
        return {
            "workstream": self.workstream,
            "period": self.period,
            "cap_usd": self.cap_usd,
            "cap_tokens": self.cap_tokens,
            "spent_usd": round(self.spent_usd, 6),
            "spent_tokens": self.spent_tokens,
            "est_usd": round(self.est_usd, 6),
            "est_tokens": self.est_tokens,
            "remaining_usd": (
                None if self.remaining_usd is None else round(self.remaining_usd, 6)
            ),
            "remaining_tokens": self.remaining_tokens,
        }

    def zone_payload(self, *, purpose: Optional[str] = None) -> dict:
        """Leak-free payload for a graduated ``budget.warn/throttle/reserve``
        event: the base :meth:`to_payload` plus the ``zone`` (+ ``purpose`` and
        reserve-buffer headroom on a reserve event). Numbers/enums only."""
        p = self.to_payload()
        z = self.zone
        p["zone"] = z
        frac = self.fraction()
        p["spent_frac"] = None if frac is None else round(frac, 6)
        if purpose is not None:
            p["purpose"] = purpose
        if z == ZONE_RESERVE:
            p["reserve_frac"] = self.reserve_frac
            p["reserve_headroom_usd"] = (
                None
                if self.reserve_headroom_usd is None
                else round(self.reserve_headroom_usd, 6)
            )
            p["reserve_headroom_tokens"] = self.reserve_headroom_tokens
        return p


# --- Period windows ---------------------------------------------------------


def _window_clause(period: str) -> str:
    """Return a SQL boolean over ``events.ts`` for ``period`` (validated → safe).

    ``period`` is checked against :data:`VALID_PERIODS`, so the returned fragment
    is composed only of constants — never caller input — and carries no params.
    """
    if period not in VALID_PERIODS:
        raise ValueError(f"invalid period {period!r} (allowed: {VALID_PERIODS})")
    if period == "daily":
        return "ts >= date_trunc('day', now())"
    if period == "monthly":
        return "ts >= date_trunc('month', now())"
    if period == "rolling_30d":
        return "ts >= now() - interval '30 days'"
    return "TRUE"  # all_time


# --- Table access -----------------------------------------------------------


def set_budget(
    conn: psycopg.Connection,
    workstream: str,
    *,
    period: str = DEFAULT_PERIOD,
    cap_usd: Optional[float] = None,
    cap_tokens: Optional[int] = None,
    warn_frac: Optional[float] = None,
    throttle_frac: Optional[float] = None,
    reserve_frac: Optional[float] = None,
) -> Budget:
    """Create or update a workstream's cap for ``period`` (idempotent upsert).

    A cap of ``None`` for a resource means that resource is unconstrained; a row
    with both ``None`` constrains nothing (but records the intent to track).

    The threshold fractions are the graduated-governance thresholds (ADR-0022);
    leaving them ``None`` keeps the OLD hard-cap-only behavior. Pass all three to
    opt in (e.g. :data:`DEFAULT_WARN_FRAC` / ``THROTTLE`` / ``RESERVE``). To make
    the org/key ceiling, pass ``workstream=`` :data:`ORG_WORKSTREAM`.
    """
    if period not in VALID_PERIODS:
        raise ValueError(f"invalid period {period!r} (allowed: {VALID_PERIODS})")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO budgets
                    (workstream, period, cap_usd, cap_tokens,
                     warn_frac, throttle_frac, reserve_frac)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (workstream, period) DO UPDATE
                    SET cap_usd = EXCLUDED.cap_usd,
                        cap_tokens = EXCLUDED.cap_tokens,
                        warn_frac = EXCLUDED.warn_frac,
                        throttle_frac = EXCLUDED.throttle_frac,
                        reserve_frac = EXCLUDED.reserve_frac,
                        updated_at = now()
                RETURNING workstream, period, cap_usd, cap_tokens,
                          warn_frac, throttle_frac, reserve_frac
                """,
                (
                    workstream, period, cap_usd, cap_tokens,
                    warn_frac, throttle_frac, reserve_frac,
                ),
            )
            row = cur.fetchone()
    return _row_to_budget(row)


_BUDGET_COLS = (
    "workstream, period, cap_usd, cap_tokens, "
    "warn_frac, throttle_frac, reserve_frac"
)


def get_budget(
    conn: psycopg.Connection, workstream: str, *, period: str = DEFAULT_PERIOD
) -> Optional[Budget]:
    """Fetch a single ``(workstream, period)`` cap, or ``None`` if none is set."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BUDGET_COLS} FROM budgets "
            "WHERE workstream = %s AND period = %s",
            (workstream, period),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return _row_to_budget(row) if row else None


def list_budgets(conn: psycopg.Connection, workstream: str) -> list[Budget]:
    """All configured caps for a workstream (one per period), period-ordered."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BUDGET_COLS} FROM budgets "
            "WHERE workstream = %s ORDER BY period",
            (workstream,),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [_row_to_budget(r) for r in rows]


def _frac(row: Any, key: str) -> Optional[float]:
    return None if row[key] is None else float(row[key])


def _row_to_budget(row: Any) -> Budget:
    return Budget(
        workstream=row["workstream"],
        period=row["period"],
        cap_usd=None if row["cap_usd"] is None else float(row["cap_usd"]),
        cap_tokens=None if row["cap_tokens"] is None else int(row["cap_tokens"]),
        warn_frac=_frac(row, "warn_frac"),
        throttle_frac=_frac(row, "throttle_frac"),
        reserve_frac=_frac(row, "reserve_frac"),
    )


# --- Accrued spend + status -------------------------------------------------


def spent(
    conn: psycopg.Connection, workstream: str, *, period: str = DEFAULT_PERIOD
) -> Spend:
    """Real accrued cost/tokens for ``workstream`` within ``period``'s window.

    Summed from the ``model.call`` events (dry-run calls included — their cost is
    computed identically), so this is the SAME cost source as
    :func:`runtime.tasks.task_cost` / ``model_rollup``, aggregated per workstream.
    """
    window = _window_clause(period)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(sum((payload->>'cost_usd')::numeric), 0) AS cost_usd,
                COALESCE(sum((payload->>'input_tokens')::bigint
                           + (payload->>'output_tokens')::bigint), 0) AS tokens,
                count(*) AS calls
            FROM events
            WHERE type = 'model.call' AND workstream = %s AND {window}
            """,
            (workstream,),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return Spend(
        cost_usd=float(row["cost_usd"]),
        tokens=int(row["tokens"]),
        calls=int(row["calls"]),
    )


def org_spent(conn: psycopg.Connection, *, period: str = DEFAULT_PERIOD) -> Spend:
    """Real accrued cost/tokens summed across ALL workstreams within ``period``.

    This is the org/key-level total the :data:`ORG_WORKSTREAM` ceiling gates on —
    same ``model.call`` cost source as :func:`spent`, without the workstream filter.
    """
    window = _window_clause(period)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                COALESCE(sum((payload->>'cost_usd')::numeric), 0) AS cost_usd,
                COALESCE(sum((payload->>'input_tokens')::bigint
                           + (payload->>'output_tokens')::bigint), 0) AS tokens,
                count(*) AS calls
            FROM events
            WHERE type = 'model.call' AND {window}
            """
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return Spend(
        cost_usd=float(row["cost_usd"]),
        tokens=int(row["tokens"]),
        calls=int(row["calls"]),
    )


def status(
    conn: psycopg.Connection,
    budget: Budget,
    *,
    est_usd: float = 0.0,
    est_tokens: int = 0,
) -> BudgetStatus:
    """Combine a cap with its accrued spend (+ a pending-call estimate).

    For the :data:`ORG_WORKSTREAM` sentinel the accrued spend is the org-wide
    total (:func:`org_spent`); for any other workstream it is that workstream's
    own accrued spend (:func:`spent`).
    """
    if budget.workstream == ORG_WORKSTREAM:
        s = org_spent(conn, period=budget.period)
    else:
        s = spent(conn, budget.workstream, period=budget.period)
    return BudgetStatus(
        workstream=budget.workstream,
        period=budget.period,
        cap_usd=budget.cap_usd,
        cap_tokens=budget.cap_tokens,
        spent_usd=s.cost_usd,
        spent_tokens=s.tokens,
        est_usd=est_usd,
        est_tokens=est_tokens,
        warn_frac=budget.warn_frac,
        throttle_frac=budget.throttle_frac,
        reserve_frac=budget.reserve_frac,
    )


def remaining(
    conn: psycopg.Connection, workstream: str, *, period: str = DEFAULT_PERIOD
) -> Optional[BudgetStatus]:
    """The workstream's remaining headroom for ``period``, or ``None`` if uncapped.

    Read via :attr:`BudgetStatus.remaining_usd` / :attr:`remaining_tokens`
    (``None`` for an unconstrained resource).
    """
    b = get_budget(conn, workstream, period=period)
    if b is None:
        return None
    return status(conn, b)


def budget_context(
    conn: psycopg.Connection,
    workstream: str,
    *,
    period: str = DEFAULT_PERIOD,
    estimated_tokens: int = 0,
    estimated_usd: float = 0.0,
) -> Optional[BudgetContext]:
    """A :class:`~runtime.policy.BudgetContext` built from REAL accrued spend.

    This is how the policy engine gets a workstream's *actual* per-workstream
    spend (not just a single task's dry-run tokens): pass the result as
    ``PolicyRequest.budget`` and ``decide`` will escalate an over-cap action to
    NEEDS_APPROVAL (🛑), consistent with :func:`enforce`. Returns ``None`` when the
    workstream has no cap for ``period`` (nothing to gate on).
    """
    b = get_budget(conn, workstream, period=period)
    if b is None:
        return None
    st = status(conn, b, est_usd=estimated_usd, est_tokens=estimated_tokens)
    return st.context()


# --- Burn-rate + projection (ADR-0022) --------------------------------------

#: Default look-back for the recent burn-rate estimate.
DEFAULT_BURN_WINDOW_MIN = 60.0


@dataclass(frozen=True)
class BurnRate:
    """Recent spend velocity for a workstream, from the ``model.call`` log.

    Rates are per-minute over the observed span of calls inside the look-back
    window (falling back to the full window when there is <2 calls to span). All
    fields are numbers — leak-free by construction."""

    workstream: str
    window_min: float
    span_min: float
    calls: int
    usd: float
    tokens: int
    usd_per_min: float
    tokens_per_min: float
    usd_per_call: float
    tokens_per_call: float


@dataclass(frozen=True)
class Projection:
    """A burn-rate projection of when a workstream will exhaust its cap.

    ``minutes_to_exhaustion`` / ``calls_to_exhaustion`` are the MIN across the
    configured resources (the tightest first to run out); ``None`` means either
    uncapped or a zero burn rate (never exhausts at the current pace). Leak-free."""

    workstream: str
    period: str
    remaining_usd: Optional[float]
    remaining_tokens: Optional[int]
    burn: BurnRate
    minutes_to_exhaustion: Optional[float]
    calls_to_exhaustion: Optional[float]


def burn_rate(
    conn: psycopg.Connection,
    workstream: str,
    *,
    window_min: float = DEFAULT_BURN_WINDOW_MIN,
    org_wide: bool = False,
) -> BurnRate:
    """Recent USD/token burn rate for ``workstream`` (or org-wide) from ``model.call``.

    Sums the last ``window_min`` minutes of ``model.call`` events and divides by
    the observed span (min-ts→now); with <2 calls the span defaults to the full
    window so a single call does not project an infinite rate.
    """
    ws_clause = "" if org_wide else "AND workstream = %s"
    params: tuple = () if org_wide else (workstream,)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                count(*) AS calls,
                COALESCE(sum((payload->>'cost_usd')::numeric), 0) AS cost_usd,
                COALESCE(sum((payload->>'input_tokens')::bigint
                           + (payload->>'output_tokens')::bigint), 0) AS tokens,
                EXTRACT(EPOCH FROM (now() - min(ts))) / 60.0 AS span_min
            FROM events
            WHERE type = 'model.call' {ws_clause}
              AND ts >= now() - (interval '1 minute' * %s)
            """,
            (*params, window_min),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    calls = int(row["calls"])
    usd = float(row["cost_usd"])
    tokens = int(row["tokens"])
    # <2 calls can't span an interval → use the full window as the denominator.
    observed = float(row["span_min"]) if row["span_min"] is not None else 0.0
    span = observed if calls >= 2 and observed > 0 else window_min
    return BurnRate(
        workstream=ORG_WORKSTREAM if org_wide else workstream,
        window_min=window_min,
        span_min=round(span, 6),
        calls=calls,
        usd=usd,
        tokens=tokens,
        usd_per_min=usd / span if span > 0 else 0.0,
        tokens_per_min=tokens / span if span > 0 else 0.0,
        usd_per_call=usd / calls if calls else 0.0,
        tokens_per_call=tokens / calls if calls else 0.0,
    )


def project_exhaustion(
    conn: psycopg.Connection,
    workstream: str,
    *,
    period: str = DEFAULT_PERIOD,
    window_min: float = DEFAULT_BURN_WINDOW_MIN,
) -> Optional[Projection]:
    """Project when ``workstream`` will exhaust its cap at the recent burn rate.

    Combines the workstream's remaining headroom (:func:`remaining`) with its
    recent :func:`burn_rate`. Returns ``None`` when the workstream has no cap for
    ``period`` (nothing to project against). A zero burn rate yields ``None`` times
    (never exhausts at the current pace).
    """
    st = remaining(conn, workstream, period=period)
    if st is None:
        return None
    br = burn_rate(
        conn, workstream, window_min=window_min,
        org_wide=(workstream == ORG_WORKSTREAM),
    )

    def _min_over(pairs: list[tuple[Optional[float], float]]) -> Optional[float]:
        vals = [rem / rate for rem, rate in pairs if rem is not None and rate > 0]
        return min(vals) if vals else None

    minutes = _min_over(
        [(st.remaining_usd, br.usd_per_min), (st.remaining_tokens, br.tokens_per_min)]
    )
    calls = _min_over(
        [(st.remaining_usd, br.usd_per_call), (st.remaining_tokens, br.tokens_per_call)]
    )
    return Projection(
        workstream=workstream,
        period=period,
        remaining_usd=st.remaining_usd,
        remaining_tokens=st.remaining_tokens,
        burn=br,
        minutes_to_exhaustion=None if minutes is None else round(minutes, 4),
        calls_to_exhaustion=None if calls is None else round(calls, 4),
    )


# --- Estimation + enforcement ----------------------------------------------


def estimate_call_tokens(messages: list[dict]) -> int:
    """A conservative pre-call token estimate for the pending call.

    Uses the same chars→tokens basis as the dry-run provider so a keyless
    estimate matches the keyless actual cost. Input + a small output allowance.
    """
    from .model.providers.base import messages_char_len

    input_tokens = max(1, messages_char_len(messages) // _CHARS_PER_TOKEN)
    output_tokens = max(1, input_tokens // _CHARS_PER_TOKEN)
    return input_tokens + output_tokens


def _budget_fingerprint(workstream: str, period: str) -> str:
    """Stable, arg-free hash so all over-cap calls collapse to ONE 🛑 approval.

    Keyed on the workstream+period (not the task) — raising a workstream's budget
    is one stakeholder decision, so repeated blocked calls must not create a pile
    of duplicate pending approvals (request_approval is idempotent per fingerprint).
    """
    material = f"budget|{workstream}|{period}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def enforce(
    conn: psycopg.Connection,
    workstream: str,
    *,
    est_usd: float = 0.0,
    est_tokens: int = 0,
    purpose: str = PURPOSE_NORMAL,
    role: str = "system",
    task_id: Optional[UUID] = None,
    sink: "EventSink",
    request_approval: Callable[..., Any] = _request_approval,
) -> list[BudgetStatus]:
    """Gate a model call against the workstream allocation AND the org ceiling
    (ADR-0022), graduated by zone.

    The call is checked against every ``(workstream, period)`` cap for
    ``workstream`` PLUS every ``(__org__, period)`` org ceiling. Each cap yields a
    :attr:`BudgetStatus.zone`; the outcome is driven by the tightest zone:

    - **over** (would cross a hard cap — allocation OR org) → emit
      ``budget.exceeded``, raise a 🛑 "raise budget" approval (ADR-0006), and raise
      :class:`OverBudget`. The caller must NOT proceed. (Pre-ADR-0022 behavior.)
    - **reserve** (in the buffer near a cap) → allowed ONLY for a
      ``wind_down`` / ``escalation`` ``purpose`` (react/pivot/escalate); a
      ``normal`` call emits ``budget.reserve`` and is WITHHELD (raises
      :class:`OverBudget`, NO approval) so the buffer is preserved.
    - **throttle** / **warn** → NON-BLOCKING; emit ``budget.throttle`` /
      ``budget.warn`` and allow.
    - **ok** → emit ``budget.checkpoint`` and allow (as today).

    A workstream with no allocation row AND no org ceiling is unconstrained:
    returns ``[]``. A cap-only row (no threshold fractions) has only ``ok`` /
    ``over`` zones, so it behaves exactly as the old hard cap. ``request_approval``
    is injectable for tests; it defaults to the real persisted approval loop.
    """
    if purpose not in VALID_PURPOSES:
        raise ValueError(f"invalid purpose {purpose!r} (allowed: {VALID_PURPOSES})")

    # Enforceable caps: this workstream's allocation rows + the org ceiling rows
    # (skip the org lookup when we ARE the org sentinel). Tracking-only rows
    # (no cap set) constrain nothing and are dropped.
    budgets = list_budgets(conn, workstream)
    if workstream != ORG_WORKSTREAM:
        budgets = budgets + list_budgets(conn, ORG_WORKSTREAM)
    entries = [
        status(conn, b, est_usd=est_usd, est_tokens=est_tokens)
        for b in budgets
        if not (b.cap_usd is None and b.cap_tokens is None)
    ]
    if not entries:
        return []

    # 1. Hard cap (allocation OR org): any 'over' blocks + raises the 🛑 approval.
    for st in entries:
        if st.zone == ZONE_OVER:
            sink.emit(
                make_event(
                    workstream=workstream,
                    type=EVENT_BUDGET_EXCEEDED,
                    task_id=task_id,
                    payload={"reason": st.exceed_reason(), **st.to_payload()},
                )
            )
            # 🛑 raising the ceiling is a stakeholder decision (ADR-0006). Idempotent
            # per (offending scope)+period so repeated blocked calls don't pile up.
            request_approval(
                conn,
                task_id=task_id,
                role=role,
                tool="model.call",
                capabilities=[],
                tier=RAISE_BUDGET_TIER,
                reason=(
                    f"raise budget for {st.workstream} ({st.period}): "
                    + st.exceed_reason()
                ),
                sink=sink,
                workstream=workstream,
                fingerprint=_budget_fingerprint(st.workstream, st.period),
            )
            raise OverBudget(st)

    # 2. Reserve zone: a 'normal' call is WITHHELD (buffer preserved); wind_down /
    #    escalation is allowed through. No approval — this is not a ceiling raise.
    if purpose not in RESERVE_PURPOSES:
        for st in entries:
            if st.zone == ZONE_RESERVE:
                sink.emit(
                    make_event(
                        workstream=workstream,
                        type=EVENT_BUDGET_RESERVE,
                        task_id=task_id,
                        payload=st.zone_payload(purpose=purpose),
                    )
                )
                raise OverBudget(st)

    # 3. Allowed: emit each cap's zone event (warn / throttle / reserve-allowed /
    #    checkpoint) and return the statuses.
    for st in entries:
        z = st.zone
        payload = (
            st.to_payload() if z == ZONE_OK else st.zone_payload(purpose=purpose)
        )
        sink.emit(
            make_event(
                workstream=workstream,
                type=_ZONE_EVENT[z],
                task_id=task_id,
                payload=payload,
            )
        )
    return entries
