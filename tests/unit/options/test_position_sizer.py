"""Tests for the seller-desk position sizer (options_spec.md §14)."""

from __future__ import annotations

from src.options.greeks_budget import GreeksBudget, PortfolioGreeks
from src.options.position_sizer import lot_size_mismatch, lots_for_risk, size_position


def test_lots_for_risk_spec_example() -> None:
    # options_spec.md §14 worked example: capital=4,00,000, risk=2%,
    # wing_width=100, credit=30, lot_size=25 -> max_loss_per_lot=1750 -> 4 lots
    lots = lots_for_risk(400_000.0, 2.0, max_loss_per_lot=1750.0)
    assert lots == 4


def test_lots_for_risk_zero_max_loss_is_zero() -> None:
    assert lots_for_risk(400_000.0, 2.0, max_loss_per_lot=0.0) == 0


def test_lot_size_mismatch() -> None:
    assert lot_size_mismatch(25, 25) is False
    assert lot_size_mismatch(25, 30) is True


def _budget() -> GreeksBudget:
    return GreeksBudget(vega=100000, delta=50000, theta=5000, gamma=80000)


def test_size_position_full_pass() -> None:
    result = size_position(
        options_capital=400_000.0,
        risk_per_trade_pct=2.0,
        max_loss_per_lot=1750.0,
        margin_per_lot=10_000.0,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(net_delta=10, net_gamma=1, net_theta=-5, net_vega=50),
        greeks_budget=_budget(),
    )
    assert result.rejected is False
    assert result.lots == 4


def test_size_position_margin_gate_binds() -> None:
    result = size_position(
        options_capital=400_000.0,
        risk_per_trade_pct=2.0,
        max_loss_per_lot=1750.0,  # risk gate would allow 4 lots
        margin_per_lot=50_000.0,
        available_margin=100_000.0,  # 90% = 90,000 -> only 1 lot fits at 50k/lot
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(net_delta=1, net_gamma=1, net_theta=1, net_vega=1),
        greeks_budget=_budget(),
    )
    assert result.rejected is False
    assert result.lots == 1


def test_size_position_greeks_gate_binds() -> None:
    result = size_position(
        options_capital=400_000.0,
        risk_per_trade_pct=2.0,
        max_loss_per_lot=1750.0,  # allows 4 lots
        margin_per_lot=1_000.0,
        available_margin=1_000_000.0,  # margin not binding
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(net_vega=40000),  # vega budget 100000 / 40000 = 2 lots
        greeks_budget=_budget(),
    )
    assert result.rejected is False
    assert result.lots == 2


def test_size_position_rejected_when_below_minimum_after_margin_gate() -> None:
    result = size_position(
        options_capital=10_000.0,
        risk_per_trade_pct=2.0,
        max_loss_per_lot=1750.0,  # 10000*0.02/1750 = 0 lots already
        margin_per_lot=1_000.0,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
    )
    assert result.rejected is True
    assert result.lots == 0


# ── full_risk_per_trade_pct rescue (2026-09-08) ──────────────────────────────
# Found live: a real NIFTY bull_put_spread's max_loss_1lot (~19,752) consumed
# ~99% of the FULL 2% budget on 1,000,000 capital (~20,000) -- low_ivr_mode's
# 0.25x reduced-size multiplier (0.5% budget, 5,000) rounds this to 0 lots
# every time, since index options have no fractional-lot sizing. These tests
# use the same real numbers.


def test_size_position_reduced_budget_rescued_to_one_lot_when_full_budget_fits() -> None:
    result = size_position(
        options_capital=1_000_000.0,
        risk_per_trade_pct=0.5,  # reduced (0.25x of the 2.0 base)
        max_loss_per_lot=19_751.88,  # reduced budget = 5,000 -> 0 lots alone
        margin_per_lot=19_751.88,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        full_risk_per_trade_pct=2.0,  # full budget = 20,000 -> fits exactly 1 lot
    )
    assert result.rejected is False
    assert result.lots == 1
    assert result.reason == "sized_rescued_to_min_lot"


def test_size_position_reduced_budget_stays_rejected_when_even_full_budget_cant_afford_it() -> None:
    result = size_position(
        options_capital=1_000_000.0,
        risk_per_trade_pct=0.5,
        max_loss_per_lot=50_000.0,  # exceeds even the full 20,000 budget
        margin_per_lot=50_000.0,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        full_risk_per_trade_pct=2.0,
    )
    assert result.rejected is True
    assert result.lots == 0


def test_size_position_no_rescue_without_full_risk_per_trade_pct() -> None:
    # Default behavior (full_risk_per_trade_pct omitted) is unchanged --
    # existing callers that don't opt in still get a plain rejection.
    result = size_position(
        options_capital=1_000_000.0,
        risk_per_trade_pct=0.5,
        max_loss_per_lot=19_751.88,
        margin_per_lot=19_751.88,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
    )
    assert result.rejected is True
    assert result.lots == 0


def test_size_position_rescue_never_exceeds_min_lot() -> None:
    # Even if the full budget could afford several lots, the rescue caps at
    # exactly 1 -- still the smallest expression of "reduced size" available,
    # never silently reverting to full-size sizing.
    result = size_position(
        options_capital=1_000_000.0,
        risk_per_trade_pct=0.1,  # reduced budget = 1,000 -> 0 lots alone
        max_loss_per_lot=2_000.0,  # full budget (20,000) would afford 10 lots
        margin_per_lot=2_000.0,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        full_risk_per_trade_pct=2.0,
    )
    assert result.rejected is False
    assert result.lots == 1


def test_size_position_rescue_not_applied_when_full_pct_not_greater() -> None:
    # full_risk_per_trade_pct <= risk_per_trade_pct is not a real reduction --
    # rescue logic must not fire (would be a no-op re-check anyway).
    result = size_position(
        options_capital=1_000_000.0,
        risk_per_trade_pct=2.0,
        max_loss_per_lot=50_000.0,
        margin_per_lot=50_000.0,
        available_margin=1_000_000.0,
        current_greeks=PortfolioGreeks(),
        per_lot_greeks=PortfolioGreeks(),
        greeks_budget=_budget(),
        full_risk_per_trade_pct=2.0,
    )
    assert result.rejected is True
    assert result.lots == 0
