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
  (real or dry-run) call: if a configured cap **would be exceeded** it emits
  ``budget.exceeded``, raises a 🛑 :func:`runtime.approvals.request_approval`
  ("raise budget for <workstream>"), and raises :class:`OverBudget` — the call
  does NOT proceed. Under cap it emits ``budget.checkpoint`` and returns.

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
from .event_types import EVENT_BUDGET_CHECKPOINT, EVENT_BUDGET_EXCEEDED
from .models import make_event
from .policy import BudgetContext

if TYPE_CHECKING:  # avoid importing the enforce module at runtime (no cycle)
    from .enforce import EventSink

# The budget wire strings (``EVENT_BUDGET_EXCEEDED`` on a blocked over-cap call,
# ``EVENT_BUDGET_CHECKPOINT`` on an under-cap spend checkpoint) are imported from
# the canonical :mod:`runtime.event_types`.

#: The default window when a caller does not name one.
DEFAULT_PERIOD = "monthly"
#: Windows a budget may be scoped to. ``all_time`` = the workstream's whole history.
VALID_PERIODS = ("daily", "monthly", "rolling_30d", "all_time")

#: Tier shown on the 🛑 "raise budget" approval — ADR-0006 "approve (blocks)".
RAISE_BUDGET_TIER = "🛑"

# Which model.call the estimate models — kept consistent with the dry-run
# provider so a keyless estimate matches the keyless actual cost.
_CHARS_PER_TOKEN = 4


class OverBudget(Exception):
    """Raised when a (real or dry-run) model call would exceed a workstream cap.

    Distinct from :class:`runtime.model.router.OverBudget` (which is about a
    *routing tier* having no cheaper fallback): this is the hard **per-workstream**
    ceiling. Carries only leak-free numbers so it is safe to log/surface.
    """

    def __init__(self, status: "BudgetStatus") -> None:
        self.status = status
        self.workstream = status.workstream
        self.period = status.period
        super().__init__(
            f"over budget for workstream {status.workstream!r} ({status.period}): "
            + status.exceed_reason()
        )


# --- Row / value models -----------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """One configured cap for a ``(workstream, period)``."""

    workstream: str
    period: str
    cap_usd: Optional[float] = None
    cap_tokens: Optional[int] = None


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
) -> Budget:
    """Create or update a workstream's cap for ``period`` (idempotent upsert).

    A cap of ``None`` for a resource means that resource is unconstrained; a row
    with both ``None`` constrains nothing (but records the intent to track).
    """
    if period not in VALID_PERIODS:
        raise ValueError(f"invalid period {period!r} (allowed: {VALID_PERIODS})")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO budgets (workstream, period, cap_usd, cap_tokens)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (workstream, period) DO UPDATE
                    SET cap_usd = EXCLUDED.cap_usd,
                        cap_tokens = EXCLUDED.cap_tokens,
                        updated_at = now()
                RETURNING workstream, period, cap_usd, cap_tokens
                """,
                (workstream, period, cap_usd, cap_tokens),
            )
            row = cur.fetchone()
    return _row_to_budget(row)


def get_budget(
    conn: psycopg.Connection, workstream: str, *, period: str = DEFAULT_PERIOD
) -> Optional[Budget]:
    """Fetch a single ``(workstream, period)`` cap, or ``None`` if none is set."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workstream, period, cap_usd, cap_tokens FROM budgets "
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
            "SELECT workstream, period, cap_usd, cap_tokens FROM budgets "
            "WHERE workstream = %s ORDER BY period",
            (workstream,),
        )
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    return [_row_to_budget(r) for r in rows]


def _row_to_budget(row: Any) -> Budget:
    return Budget(
        workstream=row["workstream"],
        period=row["period"],
        cap_usd=None if row["cap_usd"] is None else float(row["cap_usd"]),
        cap_tokens=None if row["cap_tokens"] is None else int(row["cap_tokens"]),
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


def status(
    conn: psycopg.Connection,
    budget: Budget,
    *,
    est_usd: float = 0.0,
    est_tokens: int = 0,
) -> BudgetStatus:
    """Combine a cap with its accrued spend (+ a pending-call estimate)."""
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
    role: str = "system",
    task_id: Optional[UUID] = None,
    sink: "EventSink",
    request_approval: Callable[..., Any] = _request_approval,
) -> list[BudgetStatus]:
    """Gate a model call against EVERY configured cap for ``workstream``.

    For each ``(workstream, period)`` cap: if committing the estimated call would
    break it, emit ``budget.exceeded``, raise a 🛑 "raise budget" approval, and
    raise :class:`OverBudget` — the caller must NOT proceed. If all caps have
    headroom, emit a ``budget.checkpoint`` per cap and return their statuses.

    A workstream with no ``budgets`` row is unconstrained: returns ``[]`` and does
    nothing (so the studio is unaffected until a cap is set). ``request_approval``
    is injectable for tests; it defaults to the real persisted approval loop.
    """
    budgets = list_budgets(conn, workstream)
    if not budgets:
        return []

    statuses: list[BudgetStatus] = []
    for b in budgets:
        if b.cap_usd is None and b.cap_tokens is None:
            continue  # a tracking-only row constrains nothing
        st = status(conn, b, est_usd=est_usd, est_tokens=est_tokens)
        if st.would_exceed:
            sink.emit(
                make_event(
                    workstream=workstream,
                    type=EVENT_BUDGET_EXCEEDED,
                    task_id=task_id,
                    payload={"reason": st.exceed_reason(), **st.to_payload()},
                )
            )
            # 🛑 raising the ceiling is a stakeholder decision (ADR-0006). Idempotent
            # per workstream+period so repeated blocked calls don't pile up.
            request_approval(
                conn,
                task_id=task_id,
                role=role,
                tool="model.call",
                capabilities=[],
                tier=RAISE_BUDGET_TIER,
                reason=(
                    f"raise budget for {workstream} ({b.period}): "
                    + st.exceed_reason()
                ),
                sink=sink,
                workstream=workstream,
                fingerprint=_budget_fingerprint(workstream, b.period),
            )
            raise OverBudget(st)
        statuses.append(st)

    for st in statuses:
        sink.emit(
            make_event(
                workstream=workstream,
                type=EVENT_BUDGET_CHECKPOINT,
                task_id=task_id,
                payload=st.to_payload(),
            )
        )
    return statuses
