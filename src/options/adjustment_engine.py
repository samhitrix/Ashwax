"""Seller-desk adjustment engine — cost-benefit gated position rolling
(options_spec.md §10 adjustment subsection).

Each adjustment costs money; after 2, the position is materially a different
trade than the one entered, so this forces a close rather than a 3rd
adjustment, regardless of current P&L. Actual roll construction (finding the
new strike) reuses ``src.options.strike_selector`` — this module only
answers "should we adjust or close", not "which new strike".
"""

from __future__ import annotations

from dataclasses import dataclass

_BROKERAGE_PER_ADJUSTMENT = 80.0  # Rs 20 x 4 orders (2 close + 2 open)
_MARKET_IMPACT_PCT = 0.001  # 0.1% of notional
_COST_BENEFIT_MAX_PCT = 30.0
_ADJUSTMENT_BLOCK_BELOW_DTE = 7
_MAX_ADJUSTMENTS = 2


@dataclass
class AdjustmentDecision:
    action: str  # "ADJUST" | "CLOSE"
    reason: str


def estimate_adjustment_cost(leg_spreads: list[tuple[float, float]], notional: float) -> float:
    """Sum of (ask-bid)/2 across all legs (2 close + 2 open) + fixed
    brokerage + estimated market impact (§10)."""
    spread_cost = sum((ask - bid) / 2.0 for bid, ask in leg_spreads)
    market_impact = notional * _MARKET_IMPACT_PCT
    return spread_cost + _BROKERAGE_PER_ADJUSTMENT + market_impact


def evaluate_adjustment(
    *, dte: int, adjustments_so_far: int, adjustment_cost: float, remaining_premium: float
) -> AdjustmentDecision:
    """Cost-benefit + DTE + max-count gates, in the spec's stated order (§10):
    max-count first, then DTE, then cost-benefit."""
    if adjustments_so_far >= _MAX_ADJUSTMENTS:
        return AdjustmentDecision("CLOSE", "MAX_ADJUSTMENTS_REACHED")
    if dte < _ADJUSTMENT_BLOCK_BELOW_DTE:
        return AdjustmentDecision("CLOSE", "ADJUSTMENT_BLOCKED_LOW_DTE")
    if remaining_premium <= 0.0:
        return AdjustmentDecision("CLOSE", "no remaining premium to justify adjustment cost")
    cost_pct = adjustment_cost / remaining_premium * 100.0
    if cost_pct > _COST_BENEFIT_MAX_PCT:
        return AdjustmentDecision(
            "CLOSE",
            f"ADJUSTMENT_COST_EXCEEDS_BENEFIT cost={cost_pct:.0f}% > {_COST_BENEFIT_MAX_PCT:.0f}%",
        )
    return AdjustmentDecision(
        "ADJUST", f"cost={cost_pct:.0f}% within {_COST_BENEFIT_MAX_PCT:.0f}% budget"
    )
