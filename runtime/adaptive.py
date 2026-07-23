"""Adaptive orchestration intensity — scale review/retro/research by FACTS (ADR-0003).

ADR-0003 says the cycle is **adaptive, not fixed**: "more review when a
workstream's recent error rate is high, more research in a fast-moving domain,
throttled by token/time budget". Today the worker already has *lite* triggers
(``WORKER_REVIEW=on_risk`` per-episode, ``WORKER_RETRO=on_fail``); this module
generalizes them into an evidence-based, bounded, deterministic policy that reads
the SAME telemetry the rest of the runtime records (``events``: ``verify.failed``
/ ``review.flagged`` / ``task.rekicked`` / abandonment; ``task.finished``) plus
the per-workstream budget headroom (:func:`runtime.budget.remaining`).

The contract, on purpose:

- **Evidence, not vibes.** Every knob is computed from persisted facts
  (:func:`recent_error_rate`, :func:`recent_activity`) — never from a model.
- **Deterministic + pure decision core.** :func:`_scale` / :func:`_scale_research`
  are pure functions of ``(base, error_rate, budget_fraction, activity, config)``;
  the DB readers only gather the inputs. Same inputs → same mode, always.
- **Bounded.** Every result is one of a small, closed set of legal modes — the
  same strings the worker already understands (``always`` / ``on_risk`` / ``off``
  for review, ``always`` / ``on_fail`` / ``off`` for retro).
- **Budget/time throttled.** When a workstream's budget is nearly exhausted we do
  NOT pile on extra review/retro/research — the throttle beats the escalation.
- **Off by default → behavior-preserving.** With ``ADAPTIVE_INTENSITY`` unset (or
  ``off``) every helper returns the caller's ``base_mode`` unchanged and touches
  no telemetry, so the worker's existing static behavior is preserved exactly.

All readers take an open ``conn`` (the caller owns the transaction boundary, like
:mod:`runtime.tasks` / :mod:`runtime.budget`) and read only leak-free counts —
never prompts, args, or secrets (CLAUDE.md invariants 5 & 6).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

log = logging.getLogger("runtime.adaptive")

# --- Legal, bounded mode vocabularies (mirror runtime.worker's constants) -----

#: Review trigger modes the worker understands (runtime.worker._REVIEW_MODES).
REVIEW_ALWAYS = "always"
REVIEW_ON_RISK = "on_risk"
REVIEW_OFF = "off"

#: Retro trigger modes the worker understands (runtime.worker._RETRO_MODES).
RETRO_ALWAYS = "always"
RETRO_ON_FAIL = "on_fail"
RETRO_OFF = "off"

#: Research cadence labels — how eagerly a workstream should mine external
#: best-practice. ``eager`` = research proactively (fast-moving/erroring domain),
#: ``normal`` = the baseline cadence, ``off`` = skip (clean + budget-starved).
RESEARCH_EAGER = "eager"
RESEARCH_NORMAL = "normal"
RESEARCH_OFF = "off"

#: The error-signal event types (attributed to their own task_id) that mark an
#: episode as "went wrong". These are facts the runtime already records: a failed
#: verify and a supervisor re-kick. (Abandonment + review.flagged are handled
#: separately below because they are attributed differently.)
_ERROR_EVENT_TYPES = ("verify.failed", "task.rekicked")


# --- Configuration (env defaults; deterministic thresholds) ------------------


def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes", "enabled")


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r; using default %s", name, raw, default)
        return default


@dataclass(frozen=True)
class AdaptiveConfig:
    """Bounded, env-configurable thresholds for the intensity policy.

    Every field has a sane default so a workstream needs zero configuration; a
    frozen dataclass keeps a decision reproducible (the config is a value, not
    mutable global state). :meth:`from_env` reads the process environment once.
    """

    #: Master switch. ``False`` (default) → every helper is behavior-preserving.
    enabled: bool = False
    #: How many recent WORK episodes define "recent" for the error rate.
    error_window: int = 20
    #: error_rate >= this → escalate (more review/retro). In [0, 1].
    high_error_rate: float = 0.5
    #: error_rate <= this → "clean"; combined with a tight budget → relax toward off.
    low_error_rate: float = 0.1
    #: Budget remaining fraction <= this → "tight": don't escalate to ``always``.
    budget_tight: float = 0.15
    #: Budget remaining fraction <= this → "critical": throttle hard (→ off).
    budget_critical: float = 0.05
    #: Rolling window (hours) over which domain velocity (throughput) is measured.
    velocity_window_hours: int = 24
    #: Recent WORK episodes in the velocity window >= this → "fast-moving" domain.
    high_activity: int = 8

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AdaptiveConfig":
        e = os.environ if env is None else env
        return cls(
            enabled=_env_flag(e, "ADAPTIVE_INTENSITY", cls.enabled),
            error_window=max(1, _env_int(e, "ADAPTIVE_ERROR_WINDOW", cls.error_window)),
            high_error_rate=_env_float(e, "ADAPTIVE_HIGH_ERROR_RATE", cls.high_error_rate),
            low_error_rate=_env_float(e, "ADAPTIVE_LOW_ERROR_RATE", cls.low_error_rate),
            budget_tight=_env_float(e, "ADAPTIVE_BUDGET_TIGHT", cls.budget_tight),
            budget_critical=_env_float(e, "ADAPTIVE_BUDGET_CRITICAL", cls.budget_critical),
            velocity_window_hours=max(
                1, _env_int(e, "ADAPTIVE_VELOCITY_WINDOW_HOURS", cls.velocity_window_hours)
            ),
            high_activity=max(1, _env_int(e, "ADAPTIVE_HIGH_ACTIVITY", cls.high_activity)),
        )


# --- Value helpers -----------------------------------------------------------


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def budget_fraction(budget_remaining: Any) -> Optional[float]:
    """Normalize a budget-headroom input into a remaining fraction in [0, 1].

    Accepts what :func:`runtime.budget.remaining` returns and more, so the caller
    can pass whatever it has:

    - ``None`` → ``None`` (uncapped / unknown → treated as ample; never throttles).
    - a number → that fraction, clamped to [0, 1].
    - a :class:`~runtime.budget.BudgetStatus` (duck-typed) → the MIN remaining
      fraction across its configured caps (so the tightest resource governs); a
      status with no configured cap → ``None``.
    """
    if budget_remaining is None:
        return None
    if isinstance(budget_remaining, bool):  # guard: bool is an int subclass
        return None
    if isinstance(budget_remaining, (int, float)):
        return _clamp01(float(budget_remaining))

    fracs: list[float] = []
    cap_usd = getattr(budget_remaining, "cap_usd", None)
    rem_usd = getattr(budget_remaining, "remaining_usd", None)
    if cap_usd and rem_usd is not None:  # non-None and non-zero cap
        fracs.append(rem_usd / cap_usd)
    cap_tokens = getattr(budget_remaining, "cap_tokens", None)
    rem_tokens = getattr(budget_remaining, "remaining_tokens", None)
    if cap_tokens and rem_tokens is not None:
        fracs.append(rem_tokens / cap_tokens)
    if not fracs:
        return None
    return _clamp01(min(fracs))


# --- Telemetry readers (FACTS) ----------------------------------------------


def _episode_stats(conn: Any, workstream: str, window: int) -> tuple[int, int]:
    """(episodes, errored_episodes) over the last ``window`` WORK episodes.

    An *episode* is a terminated WORK task (a ``task.finished`` event whose task is
    a ``work.*`` type — meta tasks like retro/review/research/pm do not dilute the
    signal). An episode is *errored* if it carried a ``verify.failed`` /
    ``task.rekicked`` event, was abandoned (``task.transition`` → ``abandoned``), or
    was the target of a ``review.flagged`` (whose ``target_task_id`` names the work
    episode, not the review task). Counts only — no secret text is read.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT e.task_id
                FROM events e
                JOIN tasks t ON t.id = e.task_id
                WHERE e.workstream = %(ws)s
                  AND e.type = 'task.finished'
                  AND t.type LIKE 'work.%%'
                ORDER BY e.seq DESC
                LIMIT %(win)s
            ),
            errored AS (
                SELECT task_id FROM events
                WHERE workstream = %(ws)s AND type = ANY(%(errtypes)s)
                UNION
                SELECT task_id FROM events
                WHERE workstream = %(ws)s AND type = 'task.transition'
                  AND payload->>'to' = 'abandoned'
                UNION
                SELECT (payload->>'target_task_id')::uuid AS task_id
                FROM events
                WHERE workstream = %(ws)s AND type = 'review.flagged'
                  AND payload->>'target_task_id' IS NOT NULL
            )
            SELECT
                (SELECT count(*) FROM recent) AS episodes,
                (SELECT count(DISTINCT r.task_id)
                 FROM recent r
                 WHERE r.task_id IN (SELECT task_id FROM errored)) AS errored
            """,
            {
                "ws": workstream,
                "win": int(window),
                "errtypes": list(_ERROR_EVENT_TYPES),
            },
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    episodes = int((row["episodes"] if row else 0) or 0)
    errored = int((row["errored"] if row else 0) or 0)
    return episodes, errored


def recent_error_rate(
    conn: Any,
    workstream: str,
    window: Optional[int] = None,
    *,
    config: Optional[AdaptiveConfig] = None,
) -> float:
    """Fraction of the last ``window`` WORK episodes that went wrong, in [0, 1].

    Deterministic given the DB state: ``errored_episodes / episodes`` (0.0 when a
    workstream has no recent work — no evidence → not risky). ``window`` defaults
    to :attr:`AdaptiveConfig.error_window`. This is the core FACT the review/retro
    escalation reads.
    """
    cfg = config or AdaptiveConfig.from_env()
    win = cfg.error_window if window is None else max(1, int(window))
    episodes, errored = _episode_stats(conn, workstream, win)
    if episodes == 0:
        return 0.0
    return _clamp01(errored / episodes)


def recent_activity(
    conn: Any,
    workstream: str,
    *,
    hours: Optional[int] = None,
    config: Optional[AdaptiveConfig] = None,
) -> int:
    """Count of WORK episodes finished in the last ``hours`` — a domain-velocity proxy.

    A fast-moving domain finishes many work items per unit time; that throughput is
    the closest FACT the runtime has to "domain velocity" (ADR-0003), and it drives
    :func:`research_cadence`. Counts ``task.finished`` events for ``work.*`` tasks
    within the rolling window; leak-free (a count only).
    """
    cfg = config or AdaptiveConfig.from_env()
    win_h = cfg.velocity_window_hours if hours is None else max(1, int(hours))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n
            FROM events e
            JOIN tasks t ON t.id = e.task_id
            WHERE e.workstream = %(ws)s
              AND e.type = 'task.finished'
              AND t.type LIKE 'work.%%'
              AND e.ts >= now() - make_interval(hours => %(h)s)
            """,
            {"ws": workstream, "h": int(win_h)},
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return int((row["n"] if row else 0) or 0)


# --- Pure decision core (deterministic given inputs) -------------------------


def _scale(
    base: str,
    error_rate: float,
    budget_fraction_: Optional[float],
    cfg: AdaptiveConfig,
    *,
    escalate_mode: str,
    guard_mode: str,
    off_mode: str,
) -> str:
    """The one bounded escalation rule shared by review + retro (pure).

    Priority (budget throttle beats escalation, per ADR-0003):

    1. budget *critical* → ``off_mode`` (never pile on when nearly exhausted).
    2. error rate *high* → ``escalate_mode`` (more review/retro) — but only
       ``guard_mode`` when the budget is *tight* (guard risky episodes without
       piling on).
    3. error rate *low* AND budget *tight* → ``off_mode`` (clean + starved → relax).
    4. otherwise → ``base`` unchanged.

    Result is always one of the three passed legal modes (or ``base``, itself
    legal), so the output is bounded by construction.
    """
    tight = budget_fraction_ is not None and budget_fraction_ <= cfg.budget_tight
    critical = budget_fraction_ is not None and budget_fraction_ <= cfg.budget_critical
    if critical:
        return off_mode
    if error_rate >= cfg.high_error_rate:
        return guard_mode if tight else escalate_mode
    if error_rate <= cfg.low_error_rate and tight:
        return off_mode
    return base


def _scale_research(
    base: str,
    error_rate: float,
    activity: int,
    budget_fraction_: Optional[float],
    cfg: AdaptiveConfig,
) -> str:
    """Research cadence rule (pure): mine more in a fast-moving/erroring domain,
    throttle when the budget is starved.

    1. budget *critical* → ``off``.
    2. fast-moving (activity high) OR erroring (rate high) → ``eager`` — but only
       ``normal`` when the budget is *tight*.
    3. calm (rate low, activity below threshold) AND budget *tight* → ``off``.
    4. otherwise → ``base`` (default ``normal``).
    """
    tight = budget_fraction_ is not None and budget_fraction_ <= cfg.budget_tight
    critical = budget_fraction_ is not None and budget_fraction_ <= cfg.budget_critical
    if critical:
        return RESEARCH_OFF
    fast_or_erroring = activity >= cfg.high_activity or error_rate >= cfg.high_error_rate
    if fast_or_erroring:
        return RESEARCH_NORMAL if tight else RESEARCH_EAGER
    if error_rate <= cfg.low_error_rate and activity < cfg.high_activity and tight:
        return RESEARCH_OFF
    return base


# --- Public policy entry points (DB-reading, matching ADR contract) ----------


def review_mode(
    conn: Any,
    workstream: str,
    base_mode: str,
    budget_remaining: Any = None,
    *,
    config: Optional[AdaptiveConfig] = None,
) -> str:
    """Effective review trigger mode for ``workstream`` — escalate on errors, throttle on budget.

    Returns one of ``always`` / ``on_risk`` / ``off``. When adaptive intensity is
    disabled (default) returns ``base_mode`` unchanged and reads no telemetry
    (behavior-preserving). Otherwise escalates toward ``always`` when the recent
    error rate is high and relaxes toward ``off`` when the workstream is clean and
    its budget is tight (or throttles hard when the budget is nearly exhausted).
    """
    cfg = config or AdaptiveConfig.from_env()
    if not cfg.enabled:
        return base_mode
    rate = recent_error_rate(conn, workstream, config=cfg)
    frac = budget_fraction(budget_remaining)
    return _scale(
        base_mode, rate, frac, cfg,
        escalate_mode=REVIEW_ALWAYS, guard_mode=REVIEW_ON_RISK, off_mode=REVIEW_OFF,
    )


def retro_mode(
    conn: Any,
    workstream: str,
    base_mode: str,
    budget_remaining: Any = None,
    *,
    config: Optional[AdaptiveConfig] = None,
) -> str:
    """Effective retro trigger mode for ``workstream`` — more learning when errors rise.

    Returns one of ``always`` / ``on_fail`` / ``off``. Disabled (default) →
    ``base_mode`` unchanged, no telemetry read. Enabled → escalates toward
    ``always`` on a high recent error rate (learn from every episode when things go
    wrong) and relaxes toward ``off`` when clean + budget-tight / critical.
    """
    cfg = config or AdaptiveConfig.from_env()
    if not cfg.enabled:
        return base_mode
    rate = recent_error_rate(conn, workstream, config=cfg)
    frac = budget_fraction(budget_remaining)
    return _scale(
        base_mode, rate, frac, cfg,
        escalate_mode=RETRO_ALWAYS, guard_mode=RETRO_ON_FAIL, off_mode=RETRO_OFF,
    )


def research_cadence(
    conn: Any,
    workstream: str,
    base_cadence: str = RESEARCH_NORMAL,
    budget_remaining: Any = None,
    *,
    config: Optional[AdaptiveConfig] = None,
) -> str:
    """Effective research cadence for ``workstream`` — mine more in a fast-moving domain.

    Returns one of ``eager`` / ``normal`` / ``off``. Disabled (default) →
    ``base_cadence`` unchanged, no telemetry read. Enabled → ``eager`` when the
    domain is fast-moving (high recent work throughput) or erroring (high error
    rate), throttled down when the budget is tight/critical.
    """
    cfg = config or AdaptiveConfig.from_env()
    if not cfg.enabled:
        return base_cadence
    rate = recent_error_rate(conn, workstream, config=cfg)
    activity = recent_activity(conn, workstream, config=cfg)
    frac = budget_fraction(budget_remaining)
    return _scale_research(base_cadence, rate, activity, frac, cfg)


@dataclass(frozen=True)
class IntensityDecision:
    """The resolved effective modes for one work episode + the facts behind them.

    Returned by :func:`resolve_modes` so the worker can apply the modes and log a
    leak-free rationale (counts/fractions only) — the decision is fully explained
    by its inputs.
    """

    review: str
    retro: str
    research: str
    error_rate: float
    budget_fraction: Optional[float]
    activity: int
    adaptive: bool


def resolve_modes(
    conn: Any,
    workstream: str,
    *,
    base_review: str,
    base_retro: str,
    base_research: str = RESEARCH_NORMAL,
    budget_remaining: Any = None,
    config: Optional[AdaptiveConfig] = None,
) -> IntensityDecision:
    """Resolve the effective review/retro/research modes for one work episode.

    The single seam the worker calls. When adaptive intensity is disabled (default)
    it returns the base modes verbatim and reads NO telemetry — so the worker's
    static behavior is preserved exactly. When enabled it reads the recent error
    rate + activity ONCE and derives all three modes deterministically, throttled
    by ``budget_remaining`` (as returned by :func:`runtime.budget.remaining`).
    """
    cfg = config or AdaptiveConfig.from_env()
    if not cfg.enabled:
        return IntensityDecision(
            review=base_review, retro=base_retro, research=base_research,
            error_rate=0.0, budget_fraction=None, activity=0, adaptive=False,
        )
    rate = recent_error_rate(conn, workstream, config=cfg)
    activity = recent_activity(conn, workstream, config=cfg)
    frac = budget_fraction(budget_remaining)
    return IntensityDecision(
        review=_scale(
            base_review, rate, frac, cfg,
            escalate_mode=REVIEW_ALWAYS, guard_mode=REVIEW_ON_RISK, off_mode=REVIEW_OFF,
        ),
        retro=_scale(
            base_retro, rate, frac, cfg,
            escalate_mode=RETRO_ALWAYS, guard_mode=RETRO_ON_FAIL, off_mode=RETRO_OFF,
        ),
        research=_scale_research(base_research, rate, activity, frac, cfg),
        error_rate=rate,
        budget_fraction=frac,
        activity=activity,
        adaptive=True,
    )
