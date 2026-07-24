"""Capacity telemetry eval — is capacity governance MEASURABLE? (C2, ADR-0022).

The merged budget engine (:mod:`runtime.budget`) gates spend by zone
(ok→warn→throttle→reserve→over) and projects when a workstream will exhaust its cap.
This eval proves the *measurement layer* over it works: it SEEDS a KNOWN capacity
shape directly (throwaway ``eval-capacity-*`` workstreams — a token-cap budget with
graduated thresholds + real ``model.call`` spend landing squarely in each zone), runs
the REAL telemetry (:func:`runtime.quality.capacity_report`), and asserts it recovers
that shape EXACTLY: each allocation's zone, its reserve headroom, and its
burn-projected-breach flag — with the studio-wide ``at_risk_rate`` reported as a
proper proportion carrying ``n`` + a Wilson 95% CI + the small-sample flag
(:mod:`evals.stats`), mirroring every other rate in harness v2.

Known seeded shape (cap 1000 tokens; warn/throttle/reserve = 0.70/0.85/0.90), one
workstream per target zone plus an idle (budgeted, no spend) one:

    ok=500  warn=750  throttle=860  reserve=920 (headroom 80)  over=1200  idle=0

So: 6 allocations, zones {ok:2, warn:1, throttle:1, reserve:1, over:1}, 5 projected
breaches (every spender is on track to exhaust; the idle one is not), and
at_risk_rate = 4/6 (not-ok / total).

HONEST SCOPE (mirrors the other evals): this measures the TELEMETRY mechanism — that a
known capacity standing is recovered exactly. The burn/projection use dry-run spend;
real spend velocity slots in unchanged at go-live. Seeded rows are namespaced
``eval-capacity-*`` and deleted after the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from runtime import budget
from runtime.quality import capacity_report

from .stats import Rate, rate

#: The KNOWN seeded shape: workstream suffix → spent tokens (cap is 1000 tokens).
SHAPE = {
    "ok": 500, "warn": 750, "throttle": 860, "reserve": 920, "over": 1200, "idle": 0,
}
CAP_TOKENS = 1000

#: What the telemetry must recover from :data:`SHAPE`.
EXPECTED = {
    "allocations": 6,
    "zone_counts": {"ok": 2, "warn": 1, "throttle": 1, "reserve": 1, "over": 1},
    "projected_breaches": 5,
    "reserve_headroom_tokens": 80,   # reserve: cap 1000 - spent 920
    "at_risk": 4,                    # allocations NOT in 'ok'
    "n": 6,
}


def seed_capacity_shape(conn: Any, prefix: str) -> None:
    """Seed the KNOWN budget + spend shape under the throwaway ``prefix`` workstreams."""
    from runtime.events import append_event
    from runtime.models import make_event

    for zone, tokens in SHAPE.items():
        ws = f"{prefix}-{zone}"
        budget.set_budget(
            conn, ws, cap_tokens=CAP_TOKENS,
            warn_frac=0.70, throttle_frac=0.85, reserve_frac=0.90,
        )
        if tokens:
            append_event(
                conn,
                make_event(
                    workstream=ws, type="model.call",
                    payload={
                        "model": "m", "provider": "dryrun", "role": "exec",
                        "input_tokens": tokens, "output_tokens": 0,
                        "cached_tokens": 0, "cost_usd": 0.0, "latency_ms": 1,
                    },
                ),
            )


def cleanup_capacity_shape(conn: Any, prefix: str) -> None:
    """Delete every throwaway row seeded under ``prefix`` (budgets + model.call events)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM budgets WHERE workstream LIKE %s", (f"{prefix}%",))
        cur.execute("DELETE FROM events WHERE workstream LIKE %s", (f"{prefix}%",))
    conn.commit()


@dataclass
class CapacityEvalResult:
    """Outcome of the capacity-telemetry eval: the recovered report + pass checks."""

    prefix: str
    report: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)

    def rates(self) -> list[Rate]:
        """The studio at-risk proportion as a :class:`~evals.stats.Rate` — n + Wilson
        95% CI + small-``n`` flag, exactly like every other harness rate. ``n`` = the
        budgeted allocations; the successes = those NOT in the healthy ``ok`` zone."""
        roll = self.report.get("rollup", {})
        zc = roll.get("zone_counts", {})
        n = int(self.report.get("allocations_scored", 0))
        at_risk = n - int(zc.get("ok", 0))
        return [rate("capacity_at_risk_rate", at_risk, n)]

    @property
    def passed(self) -> bool:
        """True iff every seeded-shape check matched the telemetry (mechanism proof)."""
        return all(c["ok"] for c in self.checks)

    def to_dict(self) -> dict:
        roll = self.report.get("rollup", {})
        return {
            "name": "capacity_telemetry",
            "description": (
                "Seed a KNOWN budget + spend shape and assert "
                "runtime.quality.capacity_report recovers each allocation's zone, "
                "reserve headroom, and projected-breach flag — at_risk_rate reported "
                "with n + Wilson 95% CI. Measures the TELEMETRY mechanism over the "
                "merged budget engine (ADR-0022)."
            ),
            "prefix": self.prefix,
            "rates": [r.to_dict() for r in self.rates()],
            "checks": self.checks,
            "zone_counts": roll.get("zone_counts"),
            "projected_breaches": roll.get("projected_breaches"),
            "allocations_scored": self.report.get("allocations_scored"),
            "passed": self.passed,
        }


def _check(label: str, got: Any, want: Any) -> dict:
    return {"check": label, "got": got, "want": want, "ok": got == want}


def run_capacity_eval(conn: Any, *, keep: bool = False) -> CapacityEvalResult:
    """Seed the known shape, run the real telemetry, assert recovery, then clean up.

    Needs a live ``conn`` (writes budgets + model.call events, reads the telemetry).
    Rows are namespaced ``eval-capacity-*`` and deleted afterward unless ``keep=True``."""
    prefix = f"eval-capacity-{uuid4().hex[:8]}"
    try:
        seed_capacity_shape(conn, prefix)
        report = capacity_report(conn, workstream_prefix=prefix)
        entries = {e["workstream"]: e for e in report["by_workstream"]}
        roll = report["rollup"]

        # Zone per allocation (idle is budgeted-but-empty → 'ok').
        zone_checks = [
            _check(f"zone[{z}]", entries.get(f"{prefix}-{z}", {}).get("zone"),
                   "ok" if z == "idle" else z)
            for z in SHAPE
        ]
        reserve = entries.get(f"{prefix}-reserve", {})
        idle = entries.get(f"{prefix}-idle", {})
        over = entries.get(f"{prefix}-over", {})
        checks = zone_checks + [
            _check("allocations_scored", report["allocations_scored"],
                   EXPECTED["allocations"]),
            _check("zone_counts", roll["zone_counts"], EXPECTED["zone_counts"]),
            _check("projected_breaches", roll["projected_breaches"],
                   EXPECTED["projected_breaches"]),
            _check("reserve_headroom_tokens", reserve.get("reserve_headroom_tokens"),
                   EXPECTED["reserve_headroom_tokens"]),
            # The idle (no-spend) allocation must NOT be a projected breach; a spender
            # (and the already-over one) must be — the discriminating flag.
            _check("idle_not_projected_breach",
                   idle.get("projection", {}).get("projected_breach"), False),
            _check("over_projected_breach",
                   over.get("projection", {}).get("projected_breach"), True),
            _check("at_risk_rate",
                   roll["at_risk_rate"]["rate"],
                   round(EXPECTED["at_risk"] / EXPECTED["n"], 4)),
            _check("at_risk_n", roll["at_risk_rate"]["n"], EXPECTED["n"]),
        ]
        return CapacityEvalResult(prefix=prefix, report=report, checks=checks)
    finally:
        if not keep:
            cleanup_capacity_shape(conn, prefix)


__all__ = [
    "SHAPE",
    "CAP_TOKENS",
    "EXPECTED",
    "CapacityEvalResult",
    "seed_capacity_shape",
    "cleanup_capacity_shape",
    "run_capacity_eval",
]
