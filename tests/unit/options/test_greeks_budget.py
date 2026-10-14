"""Tests for the seller-desk Greeks budget system (options_spec.md §7)."""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from src.options.greeks_budget import (
    GreeksBudget,
    PortfolioGreeks,
    compute_budget,
    last_reset,
    max_lots_within_budget,
)

_IST = ZoneInfo("Asia/Kolkata")


def test_compute_budget_defaults() -> None:
    b = compute_budget(1_000_000.0)
    assert math.isclose(b.vega, 100_000.0)
    assert math.isclose(b.delta, 50_000.0)
    assert math.isclose(b.theta, 5_000.0)
    assert math.isclose(b.gamma, 80_000.0)


def test_compute_budget_custom_cfg() -> None:
    b = compute_budget(1_000_000.0, {"vega_pct_of_capital": 20})
    assert math.isclose(b.vega, 200_000.0)


def test_last_reset_on_monday_morning() -> None:
    now = datetime(2026, 8, 3, 9, 0, tzinfo=_IST)  # Monday 09:00
    reset = last_reset(now)
    assert reset == datetime(2026, 8, 3, 8, 0, tzinfo=_IST)


def test_last_reset_before_monday_open_uses_prior_week() -> None:
    now = datetime(2026, 8, 3, 7, 0, tzinfo=_IST)  # Monday 07:00, before 08:00 reset
    reset = last_reset(now)
    assert reset == datetime(2026, 7, 27, 8, 0, tzinfo=_IST)


def test_last_reset_midweek() -> None:
    now = datetime(2026, 8, 6, 14, 0, tzinfo=_IST)  # Thursday
    reset = last_reset(now)
    assert reset == datetime(2026, 8, 3, 8, 0, tzinfo=_IST)  # preceding Monday


def test_max_lots_within_budget_unconstrained() -> None:
    budget = GreeksBudget(vega=100000, delta=50000, theta=5000, gamma=80000)
    current = PortfolioGreeks()
    per_lot = PortfolioGreeks(net_delta=100, net_gamma=10, net_theta=-50, net_vega=200)
    assert max_lots_within_budget(current, per_lot, budget, max_lots=10) == 10


def test_max_lots_within_budget_binding_constraint() -> None:
    budget = GreeksBudget(vega=1000, delta=50000, theta=5000, gamma=80000)
    current = PortfolioGreeks()
    per_lot = PortfolioGreeks(net_delta=1, net_gamma=1, net_theta=1, net_vega=300)
    # vega binds: 1000 / 300 = 3 lots
    assert max_lots_within_budget(current, per_lot, budget, max_lots=10) == 3


def test_max_lots_within_budget_zero_when_already_over() -> None:
    budget = GreeksBudget(vega=1000, delta=50000, theta=5000, gamma=80000)
    current = PortfolioGreeks(net_vega=1200)  # already over vega budget
    per_lot = PortfolioGreeks(net_vega=100)
    assert max_lots_within_budget(current, per_lot, budget, max_lots=10) == 0


def test_max_lots_within_budget_uses_absolute_value_for_short_positions() -> None:
    budget = GreeksBudget(vega=1000, delta=100, theta=5000, gamma=80000)
    current = PortfolioGreeks(net_delta=-90)  # short delta already near budget
    per_lot = PortfolioGreeks(net_delta=-5)  # adds MORE short delta
    # abs(-90)=90, room=10, per_lot abs=5 -> 2 lots
    assert max_lots_within_budget(current, per_lot, budget, max_lots=10) == 2


def test_to_rupees_scales_delta_and_gamma_but_not_theta_vega() -> None:
    from src.options.greeks_budget import to_rupees

    native = PortfolioGreeks(net_delta=2.0, net_gamma=0.001, net_theta=-300.0, net_vega=-450.0)
    rs = to_rupees(native, 24000.0)
    assert rs.net_delta == 2.0 * 24000.0 / 100.0
    assert rs.net_gamma == 0.001 * 24000.0**2 / 100.0
    assert rs.net_theta == -300.0
    assert rs.net_vega == -450.0
    assert to_rupees(native, 0.0) == PortfolioGreeks()
