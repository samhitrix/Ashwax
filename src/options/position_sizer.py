"""Seller-desk position sizing (options_spec.md §14).

Three sequential gates: margin, Greeks budget (reuses
``src.options.greeks_budget``), minimum-viable-size. Below 1 lot after all
reductions → REJECT, never round up.

Lot-size-change detection is a pure comparison here (``lot_size_mismatch``);
raising the halt incident when NSE revises a lot size is an
orchestrator/monitoring concern, not implemented in this pure module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.options.greeks_budget import GreeksBudget, PortfolioGreeks, max_lots_within_budget


@dataclass
class SizingResult:
    lots: int
    rejected: bool
    reason: str


def lots_for_risk(
    options_capital: float, risk_per_trade_pct: float, max_loss_per_lot: float
) -> int:
    """Credit-strategy sizing formula (§14): floor((capital*risk%)/max_loss_per_lot)."""
    if max_loss_per_lot <= 0.0:
        return 0
    return math.floor((options_capital * risk_per_trade_pct / 100.0) / max_loss_per_lot)


def lot_size_mismatch(stored_lot_size: int, current_lot_size: int) -> bool:
    """True if NSE has revised the lot size since a position was sized (§14)."""
    return stored_lot_size != current_lot_size


def size_position(
    options_capital: float,
    risk_per_trade_pct: float,
    max_loss_per_lot: float,
    margin_per_lot: float,
    available_margin: float,
    current_greeks: PortfolioGreeks,
    per_lot_greeks: PortfolioGreeks,
    greeks_budget: GreeksBudget,
    *,
    margin_safety_pct: float = 0.90,
    min_lots: int = 1,
    full_risk_per_trade_pct: float | None = None,
) -> SizingResult:
    """Run the 3 sequential gates (§14) and return the final approved lot count.

    ``full_risk_per_trade_pct``: the caller's UN-reduced risk_per_trade_pct,
    when ``risk_per_trade_pct`` has already been shrunk by a reduced-size
    multiplier (e.g. strategy_selector.sizing_multiplier's 0.25x/0.5x
    low-IVR bands). Found live 2026-09-08: a real NIFTY bull_put_spread's
    max_loss_1lot (~19,752) already consumes ~99% of the FULL 2% budget
    (~20,000 on 1,000,000 capital) -- any multiplier below ~1.0 rounds
    lots_for_risk() to 0 and rejects EVERY trade in that band, permanently,
    regardless of market conditions (index options have no fractional-lot
    sizing, so "smaller than 1 lot" isn't an option). If the trade would
    fit within the caller's full, unreduced budget, this rescues it to
    exactly min_lots (never more -- still the smallest expression of "reduced
    size" available) rather than silently blocking it outright. Only
    consulted when the reduced budget alone rejects; a trade that already
    sizes normally is unaffected."""
    rescued = False
    lots = lots_for_risk(options_capital, risk_per_trade_pct, max_loss_per_lot)
    if lots < min_lots:
        rescued = (
            full_risk_per_trade_pct is not None
            and full_risk_per_trade_pct > risk_per_trade_pct
            and lots_for_risk(options_capital, full_risk_per_trade_pct, max_loss_per_lot)
            >= min_lots
        )
        if not rescued:
            return SizingResult(0, True, "SIZE_TOO_SMALL_FOR_RISK_LIMIT (risk-budget gate)")
        lots = min_lots

    if margin_per_lot > 0.0:
        margin_cap_lots = int((available_margin * margin_safety_pct) / margin_per_lot)
        lots = min(lots, margin_cap_lots)
    if lots < min_lots:
        return SizingResult(0, True, "SIZE_TOO_SMALL_FOR_RISK_LIMIT (margin gate)")

    lots = max_lots_within_budget(current_greeks, per_lot_greeks, greeks_budget, max_lots=lots)
    if lots < min_lots:
        return SizingResult(0, True, "SIZE_TOO_SMALL_FOR_RISK_LIMIT (greeks gate)")

    return SizingResult(lots, False, "sized_rescued_to_min_lot" if rescued else "sized")
