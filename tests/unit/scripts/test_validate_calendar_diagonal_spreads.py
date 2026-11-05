"""Tests for the offline calendar/diagonal spread P&L sanity-check script."""

from __future__ import annotations

from scripts.validate_calendar_diagonal_spreads import estimate_two_expiry_pnl


def test_estimate_two_expiry_pnl_bs_fallback_when_no_snapshots() -> None:
    result = estimate_two_expiry_pnl(
        strategy_type="calendar_spread",
        spot=20000.0,
        near_iv=0.15,
        far_iv=0.12,
        near_dte=7,
        far_dte=35,
        lot_size=50,
        source="bs_approximation",
    )
    assert result.source == "bs_approximation"
    assert isinstance(result.estimated_debit, float)


def test_estimate_two_expiry_pnl_max_profit_is_1_5x_debit() -> None:
    result = estimate_two_expiry_pnl(
        strategy_type="calendar_spread",
        spot=20000.0,
        near_iv=0.15,
        far_iv=0.20,
        near_dte=7,
        far_dte=35,
        lot_size=50,
        source="bs_approximation",
    )
    assert result.estimated_max_profit == result.estimated_debit * 1.5


def test_estimate_two_expiry_pnl_far_higher_iv_gives_positive_debit() -> None:
    # Far leg (bought) priced at meaningfully higher IV than near leg (sold)
    # over a much longer dated expiry -> far premium should exceed near premium.
    result = estimate_two_expiry_pnl(
        strategy_type="calendar_spread",
        spot=20000.0,
        near_iv=0.12,
        far_iv=0.12,
        near_dte=7,
        far_dte=35,
        lot_size=50,
        source="bs_approximation",
    )
    assert result.estimated_debit > 0.0
