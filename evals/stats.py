"""Confidence-interval statistics for the eval harness (v2).

The stakeholder's critique of harness v1 was blunt and correct: *"precision/recall
= 1.0 on n=7 hand-seeded cases is NOT a trustworthy statistical quality metric —
it's a logic/oracle test with a huge confidence interval."* This module is the fix.
**Every rate the harness reports carries its sample size ``n`` and a Wilson 95%
confidence interval**, and a small sample (``n < 30``) is flagged as *insufficient*
so the tiny-n weakness is visible in the numbers instead of hidden behind a bare
``1.0``.

We define :func:`wilson_interval` locally (a few lines of arithmetic) rather than
importing from :mod:`runtime.quality` on purpose: the eval track and the telemetry
track stay independent, so a change to one can never silently move the other.

The Wilson score interval is used (not the naive normal approximation) because it
behaves correctly at the extremes — at ``k == n`` (a perfect score) it still yields
a proper interval strictly below 1.0 at the lower bound, which is exactly the honest
signal the stakeholder asked for. Known values (z=1.96):

- 5/5 → ≈ [0.566, 1.0]
- 7/7 → ≈ [0.646, 1.0]
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

#: Below this sample size a rate is statistically untrustworthy (wide CI) and the
#: harness flags it ``insufficient`` rather than presenting it as an estimate. 30
#: is the conventional rule-of-thumb threshold for a normal-ish sampling regime.
INSUFFICIENT_N = 30

#: z for a two-sided 95% confidence interval (the standard normal 0.975 quantile).
Z_95 = 1.959963984540054


def wilson_interval(
    successes: int, n: int, z: float = Z_95
) -> Tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion ``successes/n``.

    Returns ``(low, high)`` clamped to ``[0.0, 1.0]``. With ``n == 0`` the
    proportion is undefined, so the maximally-uninformative ``(0.0, 1.0)`` is
    returned (an honest "we know nothing"). ``successes`` is clamped into
    ``[0, n]`` defensively so a non-proportion count (e.g. re-kicks per task, which
    can exceed 1) never produces a nonsense interval.

    Known values (z=1.96): 5/5 → ≈[0.566, 1.0]; 7/7 → ≈[0.646, 1.0].
    """
    if n <= 0:
        return (0.0, 1.0)
    k = min(max(successes, 0), n)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n))
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return (low, high)


@dataclass(frozen=True)
class Rate:
    """A reported rate carrying the evidence needed to judge if it's trustworthy.

    Bundles the point estimate with its sample size ``n``, a Wilson 95% CI, and the
    ``insufficient`` flag (``n < INSUFFICIENT_N``) so **no rate is ever reported
    naked**. ``value`` is ``None`` when the denominator is 0 (undefined, never a
    divide-by-zero) — matching :func:`runtime.quality._ratio`'s convention.
    """

    label: str
    numerator: int
    denominator: int

    @property
    def n(self) -> int:
        """Sample size = the denominator of the proportion."""
        return self.denominator

    @property
    def value(self) -> Optional[float]:
        """Point estimate ``numerator/denominator``, or ``None`` if undefined."""
        return round(self.numerator / self.denominator, 4) if self.denominator else None

    @property
    def ci(self) -> Tuple[float, float]:
        """Wilson 95% CI ``(low, high)`` for the proportion."""
        return wilson_interval(self.numerator, self.denominator)

    @property
    def insufficient(self) -> bool:
        """True when ``n < INSUFFICIENT_N`` (too few samples to trust the rate)."""
        return self.denominator < INSUFFICIENT_N

    def to_dict(self) -> dict:
        """JSON-serializable form (value + n + rounded CI + insufficient flag)."""
        lo, hi = self.ci
        return {
            "label": self.label,
            "value": self.value,
            "n": self.n,
            "numerator": self.numerator,
            "ci95": [round(lo, 4), round(hi, 4)],
            "insufficient_sample": self.insufficient,
        }

    def render(self) -> str:
        """One-line human form, e.g.
        ``precision=1.0 n=5 95%CI=[0.566,1.0] INSUFFICIENT(n<30)``."""
        lo, hi = self.ci
        val = "n/a" if self.value is None else self.value
        flag = " INSUFFICIENT(n<30)" if self.insufficient else ""
        return f"{self.label}={val} n={self.n} 95%CI=[{lo:.3f},{hi:.3f}]{flag}"


def rate(label: str, numerator: int, denominator: int) -> Rate:
    """Convenience constructor for a :class:`Rate`."""
    return Rate(label=label, numerator=numerator, denominator=denominator)


__all__ = ["INSUFFICIENT_N", "Z_95", "wilson_interval", "Rate", "rate"]
