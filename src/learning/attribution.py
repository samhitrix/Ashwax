"""
Layer 2 â€” Attribution Engine: labels why each trade won or lost.

Loss axes:  REGIME_MISMATCH | TIMING_FAILURE | SIGNAL_QUALITY | NEWS_ADVERSE
Win axes:   WIN_REGIME_MATCH | WIN_TIMING_OPTIMAL | WIN_SIGNAL_QUALITY_HIGH | WIN_MARKET_TAILWIND
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from src.infrastructure.logger import get_logger
from src.learning.trade_autopsy import TradeAutopsy, TradeOutcome

logger = get_logger(__name__)


@dataclass
class AttributionSummary:
    strategy: str
    total_trades: int
    wins: int
    losses: int
    regime_mismatch_count: int
    timing_failure_count: int
    signal_quality_failure_count: int
    news_adverse_count: int
    environmental_losses: int
    skill_based_wins: int
    market_tailwind_wins: int


class AttributionEngine:
    """
    Aggregates TradeAutopsy records into attribution summaries per strategy.

    Usage:
        engine = AttributionEngine()
        engine.add(autopsy)
        summary = engine.summary("VCP_BREAKOUT")
    """

    def __init__(self) -> None:
        self._autopsies: dict[str, list[TradeAutopsy]] = defaultdict(list)

    def add(self, autopsy: TradeAutopsy) -> None:
        self._autopsies[autopsy.strategy].append(autopsy)

    def summary(self, strategy: str) -> AttributionSummary:
        return _compute_summary(strategy, self._autopsies.get(strategy, []))

    def all_summaries(self) -> list[AttributionSummary]:
        return [_compute_summary(s, r) for s, r in self._autopsies.items()]

    def dominant_failure_mode(self, strategy: str) -> str | None:
        losses = [
            r
            for r in self._autopsies.get(strategy, [])
            if r.outcome == TradeOutcome.LOSS and r.primary_failure_mode
        ]
        if not losses:
            return None
        counts: dict[str, int] = defaultdict(int)
        for r in losses:
            counts[str(r.primary_failure_mode)] += 1
        return max(counts, key=lambda k: counts[k])


def _compute_summary(strategy: str, records: list[TradeAutopsy]) -> AttributionSummary:
    wins = [r for r in records if r.outcome == TradeOutcome.WIN]
    losses = [r for r in records if r.outcome == TradeOutcome.LOSS]
    return AttributionSummary(
        strategy=strategy,
        total_trades=len(records),
        wins=len(wins),
        losses=len(losses),
        regime_mismatch_count=sum(
            1 for r in losses if str(r.primary_failure_mode) == "REGIME_MISMATCH"
        ),
        timing_failure_count=sum(
            1 for r in losses if str(r.primary_failure_mode) == "TIMING_FAILURE"
        ),
        signal_quality_failure_count=sum(
            1 for r in losses if str(r.primary_failure_mode) == "SIGNAL_QUALITY"
        ),
        news_adverse_count=sum(1 for r in losses if str(r.primary_failure_mode) == "NEWS_ADVERSE"),
        environmental_losses=sum(1 for r in losses if r.failure_was_environmental),
        skill_based_wins=sum(1 for r in wins if r.was_win_skill_based),
        market_tailwind_wins=sum(1 for r in wins if not r.was_win_skill_based),
    )
