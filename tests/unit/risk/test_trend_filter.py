"""Unit tests for src/risk/trend_filter.py — per-stock daily-trend caution."""

from __future__ import annotations

from src.risk.trend_filter import daily_trend_size_multiplier


def test_long_below_both_dmas_is_cautious() -> None:
    # Long while price below 50 & 200 DMA → confirmed downtrend → half size.
    assert daily_trend_size_multiplier(100.0, 110.0, 120.0, "BUY") == 0.5


def test_long_above_dmas_full_size() -> None:
    assert daily_trend_size_multiplier(130.0, 110.0, 120.0, "BUY") == 1.0


def test_long_below_only_50_full_size() -> None:
    # Below 50 but above 200 → not a confirmed downtrend → no caution.
    assert daily_trend_size_multiplier(115.0, 120.0, 110.0, "BUY") == 1.0


def test_short_above_both_dmas_is_cautious() -> None:
    assert daily_trend_size_multiplier(130.0, 120.0, 110.0, "SELL") == 0.5


def test_short_below_dmas_full_size() -> None:
    assert daily_trend_size_multiplier(100.0, 120.0, 110.0, "SELL") == 1.0


def test_missing_dmas_fail_open() -> None:
    assert daily_trend_size_multiplier(100.0, None, 120.0, "BUY") == 1.0
    assert daily_trend_size_multiplier(100.0, 110.0, None, "BUY") == 1.0


def test_custom_multiplier() -> None:
    assert daily_trend_size_multiplier(100.0, 110.0, 120.0, "BUY", cautious_mult=0.25) == 0.25
