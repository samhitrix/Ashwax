"""Unit tests for src/monitoring/edge_decay.py"""

from __future__ import annotations

from src.monitoring.edge_decay import DecayVerdict, TradeResult, audit_edge_decay


def _win(r: float = 1.5) -> TradeResult:
    return TradeResult(pnl_r=r)


def _loss(r: float = -1.0) -> TradeResult:
    return TradeResult(pnl_r=r)


def test_insufficient_data_no_crash():
    trades = [_win()] * 5
    result = audit_edge_decay("StratA", trades)
    assert result.strategy_name == "StratA"


def test_stable_edge():
    trades = ([_win()] * 3 + [_loss()]) * 10
    result = audit_edge_decay("S", trades, recent_window=20, baseline_window=20)
    assert result.verdict == DecayVerdict.EDGE_HEALTHY


def test_decayed_expectancy():
    baseline = [_win(2.0)] * 20
    recent = [_loss(-1.0)] * 20
    result = audit_edge_decay("S", baseline + recent, recent_window=20, baseline_window=20)
    assert result.verdict in (DecayVerdict.EDGE_GONE, DecayVerdict.RETIRED)


def test_retired_negative_expectancy():
    trades = [_loss(-1.0)] * 40
    result = audit_edge_decay("S", trades, recent_window=20, baseline_window=20)
    assert result.verdict == DecayVerdict.RETIRED


def test_degraded_partial_decay():
    baseline = [_win()] * 14 + [_loss()] * 6
    recent = [_win()] * 8 + [_loss()] * 12
    result = audit_edge_decay("S", baseline + recent, recent_window=20, baseline_window=20)
    assert result.verdict in (
        DecayVerdict.EDGE_FADING,
        DecayVerdict.EDGE_GONE,
        DecayVerdict.EDGE_HEALTHY,
    )
