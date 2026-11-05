"""Builds a real SignalQualityInput (src/intelligence/signal_quality_score.py, spec
§10) from data already computed elsewhere in this codebase — no new data sources.

Shadow-only today (config/learning.json → signal_ranker.mode): callers compute this
alongside the live 4-weight signal_ranker score and log both, so the formula can be
validated against real outcomes before it is allowed to affect ranking. See
src/execution/signal_ranker.py's module docstring and the wiring plan for the staged
rollout.

llm_confidence is NOT available at rank time — ranking happens before a candidate has
even been built into a CRMSignal, let alone run through G9. Callers pass a fixed
neutral default (0.5) for now; this makes the llm_assessment bucket (0-15 of 100)
uninformative in today's shadow logs, which is expected and documented rather than
faked as real. A post-G9 recomputation for shadow comparison is a possible follow-up,
not implemented here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.execution.signal_ranker import _follow_through_score, _volume_score
from src.intelligence.signal_quality_score import SignalQualityInput

_REGIME_SCORE = {"STRONG_BULL": 10.0, "BULL": 8.0, "NEUTRAL": 5.0, "BEAR": 2.0, "STRONG_BEAR": 0.0}
_SECTOR_STATUS_SCORE = {"LEADING": 10.0, "EMERGING": 7.0, "LAGGING": 3.0, "DECLINING": 0.0}
_NEWS_SENTIMENT_SCORE = {"BULLISH": 5.0, "NEUTRAL": 2.5, "BEARISH": 0.0}

# rs_4w (src/intelligence/sector_rotation.py) is a small ratio, not a 0-10 score.
# First-pass linear scaling, clamped — refine once shadow logs show its real
# distribution rather than guessing a better curve now.
_RS_4W_CLAMP = 0.10


def _recency_relevance(computed_at: datetime | None, now: datetime) -> float:
    """10 = backtest computed this week, 0 = 26+ weeks old or never computed."""
    if computed_at is None:
        return 0.0
    weeks = max(0.0, (now - computed_at).total_seconds() / (7 * 86400))
    return max(0.0, 10.0 - weeks / 2.6)


def _sector_strength(sector: str, sector_records: dict[str, Any]) -> float:
    record = sector_records.get(sector)
    if record is None:
        return 5.0  # unmapped/missing sector — neutral, never penalised for missing data
    rs = max(-_RS_4W_CLAMP, min(_RS_4W_CLAMP, float(getattr(record, "rs_4w", 0.0))))
    return (rs + _RS_4W_CLAMP) / (2 * _RS_4W_CLAMP) * 10.0


def build_signal_quality_input(
    *,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    is_short: bool,
    breakout_volume: float | None,
    avg_volume: float | None,
    sector: str,
    sector_status: dict[str, str],
    sector_records: dict[str, Any],
    regime: str,
    news_sentiment: str | None,
    backtest_win_rate: float | None,
    backtest_avg_r: float | None,
    backtest_computed_at: datetime | None,
    llm_confidence: float = 0.5,
    now: datetime | None = None,
) -> SignalQualityInput:
    """Real-data adapter for the spec's 0-100 Signal Quality Score.

    pattern_quality has no VCP-setup input plumbed into the scan loops yet, so it
    falls back to follow-through-in-range (close position on the signal bar) scaled
    to 0-10 — a real, non-fabricated signal, just not the spec's VCP-tightness one.
    Wiring an actual VCPSetup through is a follow-up, not built here.
    """
    _now = now or datetime.now(UTC)
    ft = _follow_through_score(bar_high, bar_low, bar_close, is_short)
    vol = _volume_score(breakout_volume, avg_volume)
    sector_status_score = _SECTOR_STATUS_SCORE.get(sector_status.get(sector, ""), 5.0)
    news_score = _NEWS_SENTIMENT_SCORE.get((news_sentiment or "").upper(), 2.5)
    return SignalQualityInput(
        pattern_quality=ft * 10.0,
        volume_confirmation=vol * 10.0,
        rs_leadership=sector_status_score,
        backtest_win_rate=float(backtest_win_rate) if backtest_win_rate is not None else 0.5,
        backtest_avg_r=float(backtest_avg_r) if backtest_avg_r is not None else 0.0,
        recency_relevance=_recency_relevance(backtest_computed_at, _now),
        regime_alignment=_REGIME_SCORE.get(regime, 5.0),
        sector_strength=_sector_strength(sector, sector_records),
        news_sentiment=news_score,
        llm_confidence=llm_confidence,
    )
