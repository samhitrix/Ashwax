"""Tests for SignalQualityScore 0-100 (spec §10)."""

from __future__ import annotations

import pytest
from src.intelligence.signal_quality_score import (
    SignalQualityInput,
    compute,
    from_context,
)


def _perfect() -> SignalQualityInput:
    return SignalQualityInput(
        pattern_quality=10,
        volume_confirmation=10,
        rs_leadership=10,
        backtest_win_rate=1.0,
        backtest_avg_r=2.0,
        recency_relevance=10,
        regime_alignment=10,
        sector_strength=10,
        news_sentiment=5,
        llm_confidence=1.0,
    )


def test_perfect_score_is_100():
    r = compute(_perfect())
    assert r.total == pytest.approx(100.0)


def test_zero_score_is_zero():
    inp = SignalQualityInput(0, 0, 0, 0.3, 0.0, 0, 0, 0, 0, 0.0)
    r = compute(inp)
    assert r.total == pytest.approx(0.0)


def test_bucket_caps():
    inp = SignalQualityInput(20, 20, 20, 2.0, 5.0, 20, 20, 20, 20, 2.0)
    r = compute(inp)
    assert r.setup_quality <= 30.0
    assert r.historical_edge <= 30.0
    assert r.context_quality <= 25.0
    assert r.llm_assessment <= 15.0
    assert r.total <= 100.0


def test_setup_quality_full():
    r = compute(_perfect())
    assert r.setup_quality == pytest.approx(30.0)


def test_llm_confidence_scales_to_15():
    r = compute(_perfect())
    assert r.llm_assessment == pytest.approx(15.0)
    inp = SignalQualityInput(0, 0, 0, 0.5, 1.0, 0, 0, 0, 0, 0.5)
    assert compute(inp).llm_assessment == pytest.approx(7.5)


def test_from_context_bull_beats_bear():
    bull = from_context(regime="BULL", llm_confidence=0.7, backtest_wr=0.55, backtest_avg_r=1.5)
    bear = from_context(regime="BEAR", llm_confidence=0.7, backtest_wr=0.55, backtest_avg_r=1.5)
    assert bull.total > bear.total


def test_from_context_positive_news_higher():
    pos = from_context(news_positive=True)
    neg = from_context(news_positive=False)
    assert pos.total > neg.total


def test_from_context_recency_decays():
    fresh = from_context(recency_weeks=4)
    stale = from_context(recency_weeks=52)
    assert fresh.total >= stale.total
