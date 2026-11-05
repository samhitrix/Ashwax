"""Unit tests for src/monitoring/cost_erosion.py"""

from __future__ import annotations

from src.monitoring.cost_erosion import ErosionLevel, TradeWithCosts, check_cost_erosion


def _t(gross: float, costs: float, risk: float = 500.0) -> TradeWithCosts:
    return TradeWithCosts(gross_pnl=gross, total_costs=costs, risk_rupees=risk)


def test_insufficient_data_returns_green():
    result = check_cost_erosion([_t(1000, 50)] * 3)
    assert result.level == ErosionLevel.GREEN
    assert "Insufficient" in result.reason


def test_green_low_cost_ratio():
    result = check_cost_erosion([_t(1000, 50)] * 5)
    assert result.level == ErosionLevel.GREEN
    assert result.cost_ratio < 0.20


def test_yellow_elevated_ratio():
    result = check_cost_erosion([_t(1000, 250)] * 5)
    assert result.level == ErosionLevel.YELLOW


def test_red_over_trading():
    result = check_cost_erosion([_t(1000, 400)] * 5)
    assert result.level == ErosionLevel.RED


def test_breakeven_r_computed():
    trades = [_t(gross=1000, costs=100, risk=500)] * 5
    result = check_cost_erosion(trades)
    assert result.breakeven_R == 0.2
