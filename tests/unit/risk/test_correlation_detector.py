"""Unit tests for src/risk/correlation_detector.py"""

from __future__ import annotations

from src.risk.correlation_detector import CorrelationLevel, detect_correlation_crisis


def _series(n: int, seed: float = 0.01) -> list[float]:
    return [seed * (1 if i % 2 == 0 else -1) for i in range(n)]


def test_fewer_than_two_symbols_returns_normal():
    result = detect_correlation_crisis({"A": _series(15)})
    assert result.level == CorrelationLevel.NORMAL
    assert "Fewer than 2" in result.reason


def test_insufficient_observations_returns_normal():
    result = detect_correlation_crisis({"A": _series(3), "B": _series(3)})
    assert result.level == CorrelationLevel.NORMAL


def test_identical_series_crisis():
    s = _series(20)
    result = detect_correlation_crisis({"A": s, "B": s})
    assert result.level == CorrelationLevel.CRISIS
    assert result.avg_correlation >= 0.85


def test_anticorrelated_series_normal():
    s = _series(20)
    neg = [-x for x in s]
    result = detect_correlation_crisis({"A": s, "B": neg})
    assert result.level == CorrelationLevel.NORMAL
    assert result.avg_correlation < 0.0


def test_three_symbols_avg_computed():
    s = _series(20)
    result = detect_correlation_crisis({"A": s, "B": s, "C": s})
    assert result.symbol_count == 3
    assert result.avg_correlation >= 0.85
