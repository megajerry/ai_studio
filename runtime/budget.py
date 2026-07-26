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
#: Synthetic output tokens as a fraction of synthetic input tokens. MUST match
#: the dry-run provider's ``_OUTPUT_RATIO`` (runtime/model/providers/dryrun.py)
#: so the keyless pre-call estimate matches the keyless actual usage SHAPE — i.e.
#: it accounts for output tokens, not input alone.
_OUTPUT_RATIO = 4


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
    #: In-flight reservation cushion (ADR-0016 / this pass): the sum of estimates
    #: for calls that have passed :func:`enforce` but whose real ``model.call`` has
    #: not landed yet. Counted toward the gate ALONGSIDE this call's own estimate so
    #: concurrent pre-checks see each other and can't collectively breach the cap.
    #: Defaults to ``0`` — a status built outside the enforce lock (``remaining`` /
    #: ``budget_context`` / the pure-logic tests) carries no cushion, so its verdict
    #: is exactly the old ``spent + est`` predicate.
    reserved_usd: float = 0.0
    reserved_tokens: int = 0

    def context(self) -> BudgetContext:
        """The equivalent :class:`~runtime.policy.BudgetContext`.

        The in-flight reservation cushion is folded into the *estimated* component
        so the shared predicate gates on ``spent + reserved + est`` vs the cap — the
        old behavior when ``reserved == 0`` (every non-enforce caller), tightened
        under concurrency when other in-flight calls have reserved.
        """
        return BudgetContext(
            spent_tokens=self.spent_tokens,
            budget_tokens=self.cap_tokens,
            estimated_tokens=self.est_tokens + self.reserved_tokens,
            spent_usd=self.spent_usd,
            budget_usd=self.cap_usd,
            estimated_usd=self.est_usd + self.reserved_usd,
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
        """Projected spent-fraction ``(spent+reserved+est)/cap`` — the MAX across
        configured caps (the tightest resource), or ``None`` if no cap is set.

        The in-flight reservation cushion is included so the zone (ADR-0022) a call
        lands in reflects OTHER concurrent in-flight calls too; it is ``0`` for any
        status built outside the enforce lock, so the fraction is the old
        ``(spent+est)/cap`` there."""
        fracs: list[float] = []
        if self.cap_usd is not None and self.cap_usd > 0:
            fracs.append(
                (self.spent_usd + self.reserved_usd + self.est_usd) / self.cap_usd
            )
        if self.cap_tokens is not None and self.cap_tokens > 0:
            fracs.append(
                (self.spent_tokens + self.reserved_tokens + self.est_tokens)
                / self.cap_tokens
            )
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

#: Minimum observed call span (minutes) the per-minute burn rate will divide by.
#: Below this the observed calls are effectively simultaneous (a near-zero span),
#: and dividing spend by ~0 would inflate ``usd_per_min`` / ``tokens_per_min`` to
#: an absurd value. When the span is below it we fall back to the full look-back
#: ``window_min`` as the denominator — exactly as we already do for <2 calls — so
#: the per-minute rate stays finite and conservative. The per-CALL figures
#: (``usd_per_call`` / ``tokens_per_call``) and ``calls_to_exhaustion`` are
#: span-independent and are therefore unaffected. ~60 ms.
MIN_SPAN_MIN = 1e-3


@dataclass(frozen=True)
class BurnRate:
    """Recent spend velocity for a workstream, from the ``model.call`` log.

    Rates are per-minute over the observed span of calls inside the look-back
    window (falling back to the full window when there is <2 calls to span, or the
    observed span is below :data:`MIN_SPAN_MIN` — a near-zero span that would
    otherwise inflate the per-minute rate). All fields are numbers — leak-free by
    construction."""

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
    the observed span (min-ts→now); with <2 calls — or a near-zero span below
    :data:`MIN_SPAN_MIN` — the span defaults to the full window so a single call
    (or a burst of near-simultaneous calls) does not project an absurd rate.
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
    # <2 calls can't span an interval, and a span below MIN_SPAN_MIN is a
    # near-zero span (near-simultaneous calls) — in BOTH cases dividing by the
    # observed span would produce a meaningless/absurd per-minute rate, so we use
    # the full window as the denominator instead. Per-call figures are unaffected.
    observed = float(row["span_min"]) if row["span_min"] is not None else 0.0
    span = observed if calls >= 2 and observed >= MIN_SPAN_MIN else window_min
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


def estimate_call_io_tokens(messages: list[dict]) -> tuple[int, int]:
    """A conservative pre-call ``(input_tokens, output_tokens)`` estimate.

    Uses the same chars→tokens basis AND output ratio as the dry-run provider so a
    keyless estimate matches the keyless actual usage shape: ``input`` =
    ``chars // _CHARS_PER_TOKEN``, ``output`` = ``input // _OUTPUT_RATIO``. Split
    out (rather than only summed) so the caller can price input and OUTPUT tokens
    SEPARATELY — output usually bills at a higher rate, so a pre-call figure that
    prices the whole sum at the input rate systematically under-counts and can let
    a call slip just past a cap.
    """
    from .model.providers.base import messages_char_len

    input_tokens = max(1, messages_char_len(messages) // _CHARS_PER_TOKEN)
    output_tokens = max(1, input_tokens // _OUTPUT_RATIO)
    return input_tokens, output_tokens


def estimate_call_tokens(messages: list[dict]) -> int:
    """A conservative pre-call TOTAL (input + output) token estimate.

    The sum of :func:`estimate_call_io_tokens`; kept for callers that only need
    the token count (the token-cap side of the budget). For the USD estimate use
    the split so output tokens are priced at the output rate (see that function).
    """
    input_tokens, output_tokens = estimate_call_io_tokens(messages)
    return input_tokens + output_tokens


def _budget_fingerprint(workstream: str, period: str) -> str:
    """Stable, arg-free hash so all over-cap calls collapse to ONE 🛑 approval.

    Keyed on the workstream+period (not the task) — raising a workstream's budget
    is one stakeholder decision, so repeated blocked calls must not create a pile
    of duplicate pending approvals (request_approval is idempotent per fingerprint).
    """
    material = f"budget|{workstream}|{period}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _spent_in_tx(
    cur: psycopg.Cursor, workstream: Optional[str], period: str
) -> Spend:
    """Real accrued spend for ``workstream`` (or org-wide when ``None``) in
    ``period``, read on an ALREADY-OPEN cursor WITHOUT committing.

    Same ``model.call`` cost source as :func:`spent` / :func:`org_spent`, but it
    runs inside the caller's transaction (the enforce row lock) so the spend read
    and the reservation write are one atomic unit — a separate committing read
    (like :func:`spent`) would break that transaction. ``workstream=None`` drops
    the workstream filter for the org ceiling total.
    """
    window = _window_clause(period)
    ws_clause = "" if workstream is None else "AND workstream = %s"
    params: tuple = () if workstream is None else (workstream,)
    cur.execute(
        f"""
        SELECT
            COALESCE(sum((payload->>'cost_usd')::numeric), 0) AS cost_usd,
            COALESCE(sum((payload->>'input_tokens')::bigint
                       + (payload->>'output_tokens')::bigint), 0) AS tokens,
            count(*) AS calls
        FROM events
        WHERE type = 'model.call' {ws_clause} AND {window}
        """,
        params,
    )
    row = cur.fetchone()
    return Spend(
        cost_usd=float(row["cost_usd"]),
        tokens=int(row["tokens"]),
        calls=int(row["calls"]),
    )


def release_reservation(
    conn: psycopg.Connection,
    workstream: str,
    *,
    est_usd: float = 0.0,
    est_tokens: int = 0,
) -> None:
    """Release an in-flight reservation made by :func:`enforce` (ADR-0016).

    Decrements ``reserved_usd`` / ``reserved_tokens`` by the SAME estimate
    :func:`enforce` reserved, on the caller's allocation rows AND the org ceiling
    (the exact set enforce incremented). Call it once the real ``model.call`` spend
    is recorded — the reservation was only a provisional cushion; real accrued spend
    is the source of truth — AND on a failed/aborted call, so a call that never
    lands can't leak a reservation that permanently shrinks the cap.

    ``GREATEST(... , 0)`` floors each column at zero so a double-release (or a
    reservation whose row appeared/vanished mid-flight) can never drive the cushion
    negative. A zero estimate is a no-op.
    """
    if est_usd == 0.0 and est_tokens == 0:
        return
    workstreams = (
        [workstream]
        if workstream == ORG_WORKSTREAM
        else [workstream, ORG_WORKSTREAM]
    )
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE budgets
                SET reserved_usd    = GREATEST(reserved_usd - %s, 0),
                    reserved_tokens = GREATEST(reserved_tokens - %s, 0),
                    updated_at = now()
                WHERE workstream = ANY(%s)
                """,
                (est_usd, est_tokens, workstreams),
            )


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

    **Concurrency (ADR-0016).** The read-then-decide used to be a TOCTOU race: N
    in-flight calls near the cap all read the same stale accrued spend and all
    passed, so their combined spend blew past the ceiling. The gate now runs the
    read + verdict + RESERVATION as one atomic step under ``SELECT ... FOR UPDATE``
    on the ``(workstream, period)`` budget row(s): each allowed call reserves its
    estimate before releasing the lock, so a concurrent call SEES it (spent +
    reserved + est) and is bounded. The reservation is provisional — release it with
    :func:`release_reservation` once the real ``model.call`` spend is recorded (or
    the call fails). Only the atomic lock+reserve is in the transaction; the
    ``budget.*`` events + 🛑 approval are emitted AFTER it commits (so a blocked
    call's telemetry is never rolled back), exactly as before.
    """
    if purpose not in VALID_PURPOSES:
        raise ValueError(f"invalid purpose {purpose!r} (allowed: {VALID_PURPOSES})")

    # Enforceable caps: this workstream's allocation rows + the org ceiling rows
    # (skip the org lookup when we ARE the org sentinel).
    workstreams = (
        [workstream]
        if workstream == ORG_WORKSTREAM
        else [workstream, ORG_WORKSTREAM]
    )

    # --- Phase 1: lock + decide + reserve, atomically. -----------------------
    # SELECT ... FOR UPDATE serializes concurrent enforces on the SAME rows, so the
    # spent+reserved read below and the reservation increment are one indivisible
    # unit. ORDER BY (workstream, period) gives a stable lock order (deadlock-safe).
    # We do NOT emit events / raise here: a rollback would erase a blocked call's
    # telemetry. We compute the verdict, (if allowed) reserve, commit, THEN act.
    entries: list[BudgetStatus] = []
    did_reserve = False  # True once the Phase-1 reservation has COMMITTED
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_BUDGET_COLS}, reserved_usd, reserved_tokens
                FROM budgets
                WHERE workstream = ANY(%s)
                ORDER BY workstream, period
                FOR UPDATE
                """,
                (workstreams,),
            )
            rows = cur.fetchall()
            for row in rows:
                b = _row_to_budget(row)
                # Tracking-only rows (no cap set) constrain nothing and are dropped.
                if b.cap_usd is None and b.cap_tokens is None:
                    continue
                is_org = b.workstream == ORG_WORKSTREAM
                s = _spent_in_tx(cur, None if is_org else b.workstream, b.period)
                entries.append(
                    BudgetStatus(
                        workstream=b.workstream,
                        period=b.period,
                        cap_usd=b.cap_usd,
                        cap_tokens=b.cap_tokens,
                        spent_usd=s.cost_usd,
                        spent_tokens=s.tokens,
                        est_usd=est_usd,
                        est_tokens=est_tokens,
                        warn_frac=b.warn_frac,
                        throttle_frac=b.throttle_frac,
                        reserve_frac=b.reserve_frac,
                        reserved_usd=None if row["reserved_usd"] is None
                        else float(row["reserved_usd"]),
                        reserved_tokens=int(row["reserved_tokens"] or 0),
                    )
                )

            if not entries:
                # Unconstrained: nothing locked to reserve on; the empty tx commits.
                over = withheld = None
            else:
                # Decide on the caller's own allocation FIRST, then the org ceiling
                # (preserving the pre-ADR-0016 evaluation order regardless of the
                # SQL sort used for lock stability).
                ordered = sorted(
                    entries, key=lambda st: (st.workstream == ORG_WORKSTREAM, st.period)
                )
                over = next((st for st in ordered if st.zone == ZONE_OVER), None)
                withheld = None
                if over is None and purpose not in RESERVE_PURPOSES:
                    withheld = next(
                        (st for st in ordered if st.zone == ZONE_RESERVE), None
                    )
                # Allowed → reserve this call's estimate on every locked cap row so
                # concurrent enforces see it. Blocked → reserve nothing.
                if over is None and withheld is None and (est_usd or est_tokens):
                    cur.execute(
                        """
                        UPDATE budgets
                        SET reserved_usd    = reserved_usd + %s,
                            reserved_tokens = reserved_tokens + %s,
                            updated_at = now()
                        WHERE workstream = ANY(%s)
                          AND NOT (cap_usd IS NULL AND cap_tokens IS NULL)
                        """,
                        (est_usd, est_tokens, workstreams),
                    )
                    did_reserve = True
    # Lock released here; an allowed call's reservation is now durable + visible.
    # ``did_reserve`` is True ONLY on the allowed path where the UPDATE ran AND the
    # transaction committed — a Phase-1 failure rolls the tx back (nothing reserved,
    # flag stays False), so a post-commit release below can never spuriously free a
    # reservation that another in-flight call holds.

    if not entries:
        return []

    # --- Phase 2: side effects, AFTER the lock/reservation committed. ---------
    # 1. Hard cap (allocation OR org): 'over' blocks + raises the 🛑 approval.
    if over is not None:
        sink.emit(
            make_event(
                workstream=workstream,
                type=EVENT_BUDGET_EXCEEDED,
                task_id=task_id,
                payload={"reason": over.exceed_reason(), **over.to_payload()},
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
                f"raise budget for {over.workstream} ({over.period}): "
                + over.exceed_reason()
            ),
            sink=sink,
            workstream=workstream,
            fingerprint=_budget_fingerprint(over.workstream, over.period),
        )
        raise OverBudget(over)

    # 2. Reserve zone: a 'normal' call is WITHHELD (buffer preserved); wind_down /
    #    escalation is allowed through. No approval — this is not a ceiling raise.
    if withheld is not None:
        sink.emit(
            make_event(
                workstream=workstream,
                type=EVENT_BUDGET_RESERVE,
                task_id=task_id,
                payload=withheld.zone_payload(purpose=purpose),
            )
        )
        raise OverBudget(withheld)

    # 3. Allowed: emit each cap's zone event (warn / throttle / reserve-allowed /
    #    checkpoint) and return the statuses. Emit in allocation-then-org order
    #    (the pre-ADR-0016 order), independent of the SQL lock sort.
    #
    # The reservation is already COMMITTED (Phase 1). If a zone-event emit raises
    # here — AFTER the commit but BEFORE we return — the caller never learns the
    # call was allowed, so it can't release the reservation and the cushion would
    # leak (permanently shrinking the cap). Guard the emit so a post-commit failure
    # releases THIS call's reservation before re-raising. Uses ``did_reserve`` so it
    # only ever undoes a reservation this call actually made (never one held by a
    # concurrent in-flight call).
    try:
        for st in sorted(
            entries, key=lambda s: (s.workstream == ORG_WORKSTREAM, s.period)
        ):
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
    except BaseException:
        if did_reserve:
            release_reservation(
                conn, workstream, est_usd=est_usd, est_tokens=est_tokens
            )
        raise
    return entries
