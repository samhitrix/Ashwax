"""
Signal Quality Score (0-100) — spec §10.

Four buckets:
  Setup Quality    (0-30): pattern quality, volume confirmation, RS leadership
  Historical Edge  (0-30): pre-trade backtest WR, avg R, recency relevance
  Context Quality  (0-25): regime alignment, sector strength, news sentiment
  LLM Assessment   (0-15): calibrated LLM confidence score
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SignalQualityInput:
    # Setup Quality (0-30)
    pattern_quality: float  # 0-10: VCP tightness / pattern clarity
    volume_confirmation: float  # 0-10: volume dryup / breakout volume
    rs_leadership: float  # 0-10: RS rank (0-100 → 0-10)

    # Historical Edge (0-30)
    backtest_win_rate: float  # 0-1 → scaled to 0-10
    backtest_avg_r: float  # R multiple → scaled to 0-10
    recency_relevance: float  # 0-10: how recent the backtest data is

    # Context Quality (0-25)
    regime_alignment: float  # 0-10: how aligned signal is with regime
    sector_strength: float  # 0-10: sector RS / rotation score
    news_sentiment: float  # 0-5: positive=5, neutral=2.5, negative=0

    # LLM Assessment (0-15)
    llm_confidence: float  # 0-1 → scaled to 0-15


@dataclass
class SignalQualityResult:
    total: float  # 0-100
    setup_quality: float  # 0-30
    historical_edge: float  # 0-30
    context_quality: float  # 0-25
    llm_assessment: float  # 0-15


def compute(inp: SignalQualityInput) -> SignalQualityResult:
    """Compute the 4-bucket signal quality score per spec §10."""
    setup = (
        _clamp(inp.pattern_quality, 0, 10)
        + _clamp(inp.volume_confirmation, 0, 10)
        + _clamp(inp.rs_leadership, 0, 10)
    )

    # backtest_win_rate 0-1 → 0-10 (50% WR = 5 pts, 70%+ = 10)
    wr_score = _clamp((inp.backtest_win_rate - 0.3) / 0.4 * 10, 0, 10)
    # avg_r: 1R=5pts, 2R+=10pts
    r_score = _clamp(inp.backtest_avg_r / 2.0 * 10, 0, 10)
    hist = wr_score + r_score + _clamp(inp.recency_relevance, 0, 10)

    ctx = (
        _clamp(inp.regime_alignment, 0, 10)
        + _clamp(inp.sector_strength, 0, 10)
        + _clamp(inp.news_sentiment, 0, 5)
    )

    llm = _clamp(inp.llm_confidence, 0.0, 1.0) * 15.0

    return SignalQualityResult(
        total=round(min(100.0, setup + hist + ctx + llm), 2),
        setup_quality=round(setup, 2),
        historical_edge=round(hist, 2),
        context_quality=round(ctx, 2),
        llm_assessment=round(llm, 2),
    )


def from_context(
    *,
    vcp_tightness: float = 5.0,
    volume_dryup: float = 5.0,
    rs_rank_pct: float = 50.0,
    backtest_wr: float = 0.5,
    backtest_avg_r: float = 1.0,
    recency_weeks: int = 52,
    regime: str = "NEUTRAL",
    sector_rs: float = 5.0,
    news_positive: bool | None = None,
    llm_confidence: float = 0.5,
) -> SignalQualityResult:
    """Convenience builder — converts raw market data to a scored result."""
    recency = max(0.0, 10.0 - recency_weeks / 26.0)  # 0 weeks=10, 52 weeks=0
    regime_score = {"STRONG_BULL": 10, "BULL": 8, "NEUTRAL": 5, "BEAR": 2, "STRONG_BEAR": 0}.get(
        regime, 5
    )
    news_score = 5.0 if news_positive is True else (0.0 if news_positive is False else 2.5)
    return compute(
        SignalQualityInput(
            pattern_quality=vcp_tightness,
            volume_confirmation=volume_dryup,
            rs_leadership=rs_rank_pct / 10.0,
            backtest_win_rate=backtest_wr,
            backtest_avg_r=backtest_avg_r,
            recency_relevance=recency,
            regime_alignment=float(regime_score),
            sector_strength=sector_rs,
            news_sentiment=news_score,
            llm_confidence=llm_confidence,
        )
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
