"""
Cost Erosion Detection — over-trading quality gate.

Detects when transaction costs are eroding a disproportionate share of gross
P&L, which is the primary symptom of over-trading or strategies with too-small
average wins relative to the cost structure.

Metrics computed:
  cost_ratio     = total_costs / abs(gross_pnl)  (lower is better)
  cost_per_trade = average cost per trade in Rs
  breakeven_R    = minimum R-multiple needed to break even after costs

Gate levels:
  GREEN   cost_ratio < 0.20   (costs < 20% of gross)
  YELLOW  cost_ratio 0.20-0.35
  RED     cost_ratio > 0.35   (costs eating > 35% of gross -- quality gate fail)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErosionLevel(StrEnum):
    GREEN = "GREEN"  # cost ratio acceptable
    YELLOW = "YELLOW"  # elevated -- monitor
    RED = "RED"  # quality gate fail -- reduce trading frequency


_GREEN_THRESHOLD = 0.20
_RED_THRESHOLD = 0.30  # spec §8.15: costs > 30% of gross = over-trading (was 0.35 — fixed)
_MIN_TRADES = 5  # need at least this many trades for a meaningful reading


@dataclass
class TradeWithCosts:
    """Minimal trade record needed for cost erosion analysis."""

    gross_pnl: float  # before costs (can be negative)
    total_costs: float  # all-in costs (always positive)
    risk_rupees: float  # planned risk per trade (1R)


@dataclass
class ErosionReport:
    level: ErosionLevel
    trade_count: int
    total_gross_pnl: float
    total_costs: float
    cost_ratio: float  # total_costs / abs(total_gross_pnl)
    avg_cost_per_trade: float  # Rs per trade
    breakeven_R: float  # R-multiple at which trade covers its own cost
    reason: str


def check_cost_erosion(
    trades: list[TradeWithCosts],
    max_cost_pct_of_gross: float = _RED_THRESHOLD,
) -> ErosionReport:
    """
    Analyse cost erosion across a list of completed trades.

    max_cost_pct_of_gross: RED threshold from risk.json cost_tracking.max_cost_pct_of_gross / 100.
        Defaults to module constant (0.35). Pass 0.30 when wired from risk.json.
    """
    if len(trades) < _MIN_TRADES:
        return ErosionReport(
            level=ErosionLevel.GREEN,
            trade_count=len(trades),
            total_gross_pnl=0.0,
            total_costs=0.0,
            cost_ratio=0.0,
            avg_cost_per_trade=0.0,
            breakeven_R=0.0,
            reason=f"Insufficient data (need >= {_MIN_TRADES} trades)",
        )

    total_gross = sum(t.gross_pnl for t in trades)
    total_costs = sum(t.total_costs for t in trades)
    avg_cost = total_costs / len(trades)
    avg_risk = sum(t.risk_rupees for t in trades) / len(trades)

    abs_gross = abs(total_gross)
    cost_ratio = total_costs / abs_gross if abs_gross > 0 else float("inf")

    breakeven_R = avg_cost / avg_risk if avg_risk > 0 else 0.0

    red_threshold = (
        max_cost_pct_of_gross  # from risk.json cost_tracking.max_cost_pct_of_gross / 100
    )
    if cost_ratio > red_threshold:
        level = ErosionLevel.RED
        reason = (
            f"Cost ratio {cost_ratio:.1%} > RED threshold {red_threshold:.0%}"
            f" -- costs eroding {cost_ratio:.0%} of gross P&L."
            f" Reduce trade frequency or increase minimum R target."
        )
    elif cost_ratio > _GREEN_THRESHOLD:
        level = ErosionLevel.YELLOW
        reason = (
            f"Cost ratio {cost_ratio:.1%} -- elevated (>{_GREEN_THRESHOLD:.0%})."
            f" Monitor. Breakeven requires {breakeven_R:.2f}R per trade."
        )
    else:
        level = ErosionLevel.GREEN
        reason = (
            f"Cost ratio {cost_ratio:.1%} -- acceptable. Breakeven {breakeven_R:.2f}R per trade."
        )

    return ErosionReport(
        level=level,
        trade_count=len(trades),
        total_gross_pnl=round(total_gross, 2),
        total_costs=round(total_costs, 2),
        cost_ratio=round(cost_ratio, 4),
        avg_cost_per_trade=round(avg_cost, 2),
        breakeven_R=round(breakeven_R, 3),
        reason=reason,
    )
