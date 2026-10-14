"""Unit tests for src/risk/real_pnl.py"""

from __future__ import annotations

import pytest
from src.risk.real_pnl import ProductType, calculate_real_pnl


def test_delivery_long_basic():
    result = calculate_real_pnl("RELIANCE", 2500.0, 2600.0, 10, ProductType.DELIVERY)
    assert result.gross_pnl == pytest.approx(1000.0)
    assert result.net_pnl < result.gross_pnl
    assert result.costs.total > 0
    assert result.costs.dp_charge == pytest.approx(15.93)


def test_intraday_no_dp_charge():
    result = calculate_real_pnl("NIFTY50", 18000.0, 18050.0, 5, ProductType.INTRADAY)
    assert result.costs.dp_charge == 0.0


def test_delivery_loss_net_pnl_more_negative():
    result = calculate_real_pnl("TCS", 3000.0, 2900.0, 5, ProductType.DELIVERY)
    assert result.gross_pnl == pytest.approx(-500.0)
    assert result.net_pnl < result.gross_pnl


def test_short_intraday_win():
    result = calculate_real_pnl("HDFC", 100.0, 90.0, 50, ProductType.INTRADAY, is_short=True)
    assert result.gross_pnl == pytest.approx(500.0)
    assert result.net_pnl < result.gross_pnl


def test_stt_delivery_vs_intraday():
    r_del = calculate_real_pnl("X", 1000.0, 1000.0, 100, ProductType.DELIVERY)
    r_mis = calculate_real_pnl("X", 1000.0, 1000.0, 100, ProductType.INTRADAY)
    assert r_del.costs.stt > r_mis.costs.stt


def test_net_pnl_pct_positive_trade():
    result = calculate_real_pnl("INFY", 1500.0, 1600.0, 20, ProductType.DELIVERY)
    assert result.net_pnl_pct > 0
