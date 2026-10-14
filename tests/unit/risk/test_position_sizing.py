"""Unit tests for src/risk/position_sizing.py."""

from __future__ import annotations

import pytest
from src.risk.position_sizing import calculate_quantity, intraday_position_size


def test_normal_long_trade() -> None:
    # entry=1000, sl=980 → sl_distance=20, capital=2000 → qty=100
    assert calculate_quantity(1000.0, 980.0, 2000.0) == 100


def test_normal_short_trade() -> None:
    # sl above entry for short; abs handles it
    # sl_distance=50, capital=2500 → qty=50
    assert calculate_quantity(1000.0, 1050.0, 2500.0) == 50


def test_minimum_qty_floor() -> None:
    # capital too small to buy even 1 unit → returns 1
    assert calculate_quantity(1000.0, 900.0, 10.0) == 1


def test_exact_fit() -> None:
    # sl_distance=50, capital=2000 → qty=40 exactly
    assert calculate_quantity(1000.0, 950.0, 2000.0) == 40


def test_zero_sl_distance_raises() -> None:
    with pytest.raises(ValueError, match="SL distance must be positive"):
        calculate_quantity(1000.0, 1000.0, 2000.0)


def test_large_capital_small_sl_distance() -> None:
    # sl_distance=1, capital=50000 → qty=50000
    assert calculate_quantity(1000.0, 999.0, 50000.0) == 50000


def test_fractional_truncation() -> None:
    # sl_distance=30, capital=1000 → 1000/30=33.33 → truncates to 33
    assert calculate_quantity(1000.0, 970.0, 1000.0) == 33


# ── intraday_position_size ───────────────────────────────────────────────────


def test_intraday_risk_cap_binds_when_funds_ample() -> None:
    # RELIANCE-like: entry 1328, SL 1322 (₹6 risk), ₹1000 risk cap → 166 sh.
    # Leveraged funds 68942*5=344710 → afford 259 sh. Risk cap binds → 166.
    assert intraday_position_size(1328.0, 1322.0, 1000.0, 68942.0 * 5) == 166


def test_intraday_affordability_binds_when_funds_tight() -> None:
    # No leverage, tiny cash: entry 1328, risk cap would allow 166 but only
    # ₹20000 funds → 20000/1328 = 15 sh affordable → 15 binds.
    assert intraday_position_size(1328.0, 1322.0, 1000.0, 20000.0) == 15


def test_intraday_zero_when_cannot_afford_one_share() -> None:
    # Funds below one share price → 0 (caller must skip the trade).
    assert intraday_position_size(1328.0, 1322.0, 1000.0, 500.0) == 0


def test_intraday_never_sizes_above_risk_cap() -> None:
    # Huge funds must not exceed the risk-based ceiling (₹1000 / ₹6 = 166).
    assert intraday_position_size(1328.0, 1322.0, 1000.0, 10_000_000.0) == 166


def test_intraday_invalid_prices_return_zero() -> None:
    assert intraday_position_size(0.0, 1322.0, 1000.0, 50000.0) == 0
    assert intraday_position_size(1328.0, 0.0, 1000.0, 50000.0) == 0
