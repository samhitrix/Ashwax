"""Seller-desk Greeks budget system (options_spec.md §7).

Weekly budgets as a percentage of options_capital, reset every Monday 08:00
IST. The pre-trade check caps proposed lots (or rejects) so the portfolio
never exceeds any of the 4 budgets — this is Gate 2 of position_sizer.py's
3-gate sizing, run atomically alongside the margin gate.

Exposure is compared using absolute value on all 4 Greeks (not just theta) —
a conservative choice that doesn't assume beneficial netting between the
existing portfolio and a proposed new position, matching the mission's
"prefer robustness over complexity" principle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_RESET_HOUR = 8  # 08:00 IST, Monday


@dataclass
class GreeksBudget:
    vega: float
    delta: float
    theta: float
    gamma: float


@dataclass
class PortfolioGreeks:
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0


def to_rupees(native: PortfolioGreeks, spot: float) -> PortfolioGreeks:
    """Convert native-unit Greeks to the rupee scale the budgets are expressed in
    (options_spec.md section 8 "Aggregation (Rupee-Scaled)").

    ``native`` is the sum of ``sign x bs_greeks x lot_size`` -- delta in underlying
    points, gamma in points^-1, theta in rupees/day, vega in rupees per vol point.
    Theta and vega are already rupees; delta becomes rupees per 1% underlying move
    (x spot/100) and gamma becomes x spot^2/100, exactly as the spec's aggregation.
    Comparing the native delta/gamma straight against a rupee budget (the old
    behaviour) made those two budgets ~100-1000x too loose to ever bind.
    """
    if spot <= 0.0:
        return PortfolioGreeks()
    return PortfolioGreeks(
        net_delta=native.net_delta * spot / 100.0,
        net_gamma=native.net_gamma * spot * spot / 100.0,
        net_theta=native.net_theta,
        net_vega=native.net_vega,
    )


def compute_budget(options_capital: float, cfg: dict[str, float] | None = None) -> GreeksBudget:
    c = cfg or {}
    return GreeksBudget(
        vega=options_capital * float(c.get("vega_pct_of_capital", 10)) / 100.0,
        delta=options_capital * float(c.get("delta_pct_of_capital", 5)) / 100.0,
        theta=options_capital * float(c.get("theta_pct_of_capital_per_day", 0.5)) / 100.0,
        gamma=options_capital * float(c.get("gamma_pct_of_capital", 8)) / 100.0,
    )


def last_reset(now: datetime) -> datetime:
    """Most recent Monday 08:00 (in `now`'s timezone) at or before `now`."""
    candidate = now.replace(hour=_RESET_HOUR, minute=0, second=0, microsecond=0) - timedelta(
        days=now.weekday()
    )
    if candidate > now:
        candidate -= timedelta(days=7)
    return candidate


def max_lots_within_budget(
    current: PortfolioGreeks, per_lot: PortfolioGreeks, budget: GreeksBudget, max_lots: int
) -> int:
    """Max additional lots (0..max_lots) that keep the portfolio within budget
    across all 4 Greeks simultaneously."""
    dims = (
        (abs(current.net_delta), abs(per_lot.net_delta), budget.delta),
        (abs(current.net_gamma), abs(per_lot.net_gamma), budget.gamma),
        (abs(current.net_theta), abs(per_lot.net_theta), budget.theta),
        (abs(current.net_vega), abs(per_lot.net_vega), budget.vega),
    )
    fits = max_lots
    for current_abs, per_lot_abs, cap in dims:
        if per_lot_abs <= 0.0:
            continue
        room = cap - current_abs
        if room <= 0.0:
            return 0
        fits = min(fits, int(room / per_lot_abs))
    return max(0, fits)
