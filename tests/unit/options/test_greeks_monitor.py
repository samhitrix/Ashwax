"""Tests for the seller-desk portfolio Greeks monitor (options_spec.md §8)."""

from __future__ import annotations

import math

from src.options.greeks_monitor import (
    LegGreekInputs,
    aggregate_portfolio_greeks,
    check_delta_budget,
    check_gamma_budget,
    check_vega_budget,
    daily_pnl_attribution,
)


def _leg(
    position_id: str, *, delta=0.0, gamma=0.0, theta=0.0, vega=0.0, qty=1, lot_size=25
) -> LegGreekInputs:
    return LegGreekInputs(position_id, delta, gamma, theta, vega, qty, lot_size)


def test_aggregate_portfolio_greeks() -> None:
    legs = [_leg("P1", delta=0.5, gamma=0.002, theta=-5.0, vega=10.0, qty=1, lot_size=25)]
    result = aggregate_portfolio_greeks(legs, underlying_price=24000.0)
    assert math.isclose(result.net_delta_rs, 0.5 * 1 * 25 * 24000.0 / 100.0)
    assert math.isclose(result.net_gamma_rs, 0.002 * 1 * 25 * 24000.0**2 / 100.0)
    assert math.isclose(result.net_theta_rs, -5.0 * 25)
    assert math.isclose(result.net_vega_rs, 10.0 * 25)


def test_check_delta_budget_ok() -> None:
    legs = [_leg("P1", delta=0.01, qty=1, lot_size=25)]
    portfolio = aggregate_portfolio_greeks(legs, 24000.0)
    advisory = check_delta_budget(portfolio, delta_budget=1_000_000.0)
    assert advisory.level == "OK"


def test_check_delta_budget_advisory_at_80pct() -> None:
    portfolio = aggregate_portfolio_greeks(
        [_leg("P1", delta=1.0, qty=1, lot_size=1)], underlying_price=8500.0
    )
    # net_delta_rs = 1*1*1*8500/100 = 85; budget=100 -> 85% > 80% threshold
    advisory = check_delta_budget(portfolio, delta_budget=100.0)
    assert advisory.level == "ADVISORY"


def test_check_delta_budget_breach() -> None:
    portfolio = aggregate_portfolio_greeks(
        [_leg("P1", delta=1.0, qty=1, lot_size=1)], underlying_price=15000.0
    )
    advisory = check_delta_budget(portfolio, delta_budget=100.0)
    assert advisory.level == "BREACH"
    assert "not auto-executed" in advisory.message


def test_check_vega_budget_identifies_largest_contributor() -> None:
    legs = [
        _leg("SMALL", vega=1.0, qty=1, lot_size=25),
        _leg("BIG", vega=50.0, qty=1, lot_size=25),
    ]
    portfolio = aggregate_portfolio_greeks(legs, 24000.0)
    advisory = check_vega_budget(portfolio, vega_budget=100.0, legs=legs)
    assert advisory.level in ("ADVISORY", "BREACH")
    assert advisory.largest_contributor_position_id == "BIG"


def test_check_gamma_budget_breach_identifies_contributor() -> None:
    legs = [
        _leg("SMALL", gamma=0.0001, qty=1, lot_size=25),
        _leg("BIG", gamma=0.05, qty=1, lot_size=25),
    ]
    portfolio = aggregate_portfolio_greeks(legs, 24000.0)
    advisory = check_gamma_budget(portfolio, gamma_budget=1.0, legs=legs)
    assert advisory.level == "BREACH"
    assert advisory.largest_contributor_position_id == "BIG"


def test_daily_pnl_attribution() -> None:
    from src.options.greeks_monitor import PortfolioGreeksRs

    portfolio = PortfolioGreeksRs(
        net_delta_rs=100.0, net_gamma_rs=2.0, net_theta_rs=-50.0, net_vega_rs=30.0
    )
    attribution = daily_pnl_attribution(portfolio, underlying_daily_move=10.0, daily_iv_change=0.02)
    assert math.isclose(attribution.delta_pnl, 1000.0)
    assert math.isclose(attribution.gamma_pnl, 100.0)  # 0.5*2*10^2
    assert math.isclose(attribution.theta_pnl, -50.0)
    assert math.isclose(attribution.vega_pnl, 0.6)
    assert math.isclose(attribution.total, 1000.0 + 100.0 - 50.0 + 0.6)


def test_pnl_attribution_native_uses_points_and_vol_points() -> None:
    from src.options.greeks_budget import PortfolioGreeks
    from src.options.greeks_monitor import pnl_attribution_native

    native = PortfolioGreeks(net_delta=-2.0, net_gamma=-0.002, net_theta=500.0, net_vega=-300.0)
    a = pnl_attribution_native(native, spot_move=100.0, iv_change_points=1.5, days=1.0)
    assert a.delta_pnl == -200.0  # -2 x 100 points
    assert a.gamma_pnl == 0.5 * -0.002 * 100.0**2  # -10
    assert a.theta_pnl == 500.0
    assert a.vega_pnl == -450.0  # -300 x 1.5 vol points
