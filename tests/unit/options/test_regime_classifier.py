"""Tests for the seller-desk 4-regime classifier (options_spec.md §3)."""

from __future__ import annotations

import math

import pytest
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


@pytest.mark.parametrize(
    ("vix", "adx", "trend", "expected"),
    [
        (20.0, 22.0, 0.50, "NORMAL_VOL_TRANSITIONAL"),  # the live ~86% case: VIX 15-25
        (15.2, 12.0, 0.20, "NORMAL_VOL_RANGE_BOUND"),
        (22.0, 30.0, 0.80, "NORMAL_VOL_TRENDING"),
        (12.0, 22.0, 0.50, "LOW_VOL_TRANSITIONAL"),  # low VIX but ADX 18-25
        (30.0, 22.0, 0.50, "HIGH_VOL_TRANSITIONAL"),  # high VIX but ADX 20-25
    ],
)
def test_gap_bands_are_labelled_and_stay_no_trade(
    vix: float, adx: float, trend: float, expected: str
) -> None:
    r = classify_regime(vix=vix, adx=adx, trend_score=trend, revert_score=1.0 - trend)
    assert r.regime == expected
    # label-only: identical flags to the old UNDEFINED, so no trade decision changes
    assert (r.allowed_selling, r.allowed_directional, r.sizing_pct) == (False, False, 0.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_undefined_only_for_non_finite_inputs(bad: float) -> None:
    r = classify_regime(vix=bad, adx=22.0, trend_score=0.50, revert_score=0.50)
    assert r.regime == "UNDEFINED"
    assert r.allowed_selling is False and r.allowed_directional is False


def test_original_four_regimes_unchanged_at_the_boundaries() -> None:
    # vix exactly 15 / 25 are NORMAL (spec bands are strict < 15 and > 25)
    assert classify_regime(15.0, 12.0, 0.2, 0.8).regime == "NORMAL_VOL_RANGE_BOUND"
    assert classify_regime(25.0, 12.0, 0.2, 0.8).regime == "NORMAL_VOL_RANGE_BOUND"
    assert classify_regime(14.9, 12.0, 0.2, 0.8).regime == "LOW_VOL_RANGE_BOUND"
    assert classify_regime(25.1, 15.0, 0.2, 0.8).regime == "HIGH_VOL_RANGE_BOUND"
