"""Tests for the seller-desk 4-regime classifier (options_spec.md §3)."""

from __future__ import annotations

import math

from src.options.regime_classifier import (
    classify_regime,
    mean_revert_score,
    trend_direction,
    trending_score,
    wilder_adx,
)


def _trending_ohlc(n: int = 40) -> tuple[list[float], list[float], list[float]]:
    close = [100.0 + i * 2.0 for i in range(n)]
    high = [c + 1.0 for c in close]
    low = [c - 1.0 for c in close]
    return high, low, close


def _choppy_ohlc(n: int = 40) -> tuple[list[float], list[float], list[float]]:
    close = [100.0 + 3.0 * math.sin(i * 0.9) for i in range(n)]
    high = [c + 1.0 for c in close]
    low = [c - 1.0 for c in close]
    return high, low, close


def test_wilder_adx_insufficient_bars_returns_none() -> None:
    assert wilder_adx([1.0, 2.0], [1.0, 2.0], [1.0, 2.0]) is None


def test_wilder_adx_high_for_strong_trend() -> None:
    high, low, close = _trending_ohlc()
    adx = wilder_adx(high, low, close)
    assert adx is not None
    assert adx > 25.0


def test_wilder_adx_low_for_choppy_series() -> None:
    high, low, close = _choppy_ohlc()
    adx = wilder_adx(high, low, close)
    assert adx is not None
    assert adx < 25.0


def test_trending_score_perfect_line_is_one() -> None:
    closes = [100.0 + i * 1.5 for i in range(25)]
    score = trending_score(closes)
    assert score is not None
    assert math.isclose(score, 1.0, abs_tol=1e-9)


def test_trending_score_insufficient_data_is_none() -> None:
    assert trending_score([100.0, 101.0], lookback=20) is None


def test_trending_score_low_for_noisy_series() -> None:
    closes = [100.0 + 5.0 * math.sin(i * 1.7) for i in range(25)]
    score = trending_score(closes)
    assert score is not None
    assert score < 0.5


def test_trend_direction_positive_for_uptrend() -> None:
    closes = [100.0 + i * 1.5 for i in range(25)]
    d = trend_direction(closes)
    assert d is not None
    assert d > 0.0


def test_trend_direction_negative_for_downtrend() -> None:
    closes = [100.0 - i * 1.5 for i in range(25)]
    d = trend_direction(closes)
    assert d is not None
    assert d < 0.0


def test_trend_direction_insufficient_data_is_none() -> None:
    assert trend_direction([100.0, 101.0], lookback=20) is None


def test_mean_revert_score_is_complement() -> None:
    assert math.isclose(mean_revert_score(0.8), 0.2)
    assert math.isclose(mean_revert_score(0.0), 1.0)


def test_classify_low_vol_trending() -> None:
    r = classify_regime(vix=12.0, adx=30.0, trend_score=0.80, revert_score=0.20)
    assert r.regime == "LOW_VOL_TRENDING"
    assert r.allowed_directional is True
    assert r.allowed_selling is False


def test_classify_low_vol_range_bound() -> None:
    r = classify_regime(vix=12.0, adx=15.0, trend_score=0.20, revert_score=0.80)
    assert r.regime == "LOW_VOL_RANGE_BOUND"
    assert r.allowed_selling is True
    assert r.allowed_directional is False


def test_classify_high_vol_trending() -> None:
    r = classify_regime(vix=30.0, adx=35.0, trend_score=0.75, revert_score=0.25)
    assert r.regime == "HIGH_VOL_TRENDING"
    assert r.allowed_selling is False
    assert r.sizing_pct < 100.0


def test_classify_high_vol_range_bound() -> None:
    r = classify_regime(vix=30.0, adx=15.0, trend_score=0.20, revert_score=0.80)
    assert r.regime == "HIGH_VOL_RANGE_BOUND"
    assert r.allowed_selling is True
    assert r.sizing_pct > 100.0


def test_classify_undefined_fallback() -> None:
    # vix=20 sits outside both <15 and >25 bands -> no regime matches
    r = classify_regime(vix=20.0, adx=22.0, trend_score=0.50, revert_score=0.50)
    assert r.regime == "UNDEFINED"
    assert r.allowed_selling is False
    assert r.allowed_directional is False
