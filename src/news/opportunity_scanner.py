"""
News Opportunity Scanner - generates NEWS_DRIVEN signals from high-magnitude news,
both bullish (long) and bearish (short).

A NEWS_DRIVEN signal is a suggestion; it still passes through the full CRM 10-gate pipeline.

Criteria:
  LONG:  trading_action == BOOST,        magnitude == HIGH, sentiment == BULLISH, confidence >= 0.65
  SHORT: trading_action in (AVOID, HALT), magnitude == HIGH, sentiment == BEARISH, confidence >= 0.65

The classifier's action vocabulary is framed defensively for bearish news (AVOID/HALT -
"stay out of this name") rather than "SHORT" - there is no dedicated bullish-style action
for the bearish case. AVOID/HALT + HIGH magnitude + BEARISH sentiment is, in practice,
the same signal shape as BOOST + HIGH + BULLISH: a real, high-confidence event that just
moved the stock hard. Reusing it here (rather than inventing a new classifier action)
keeps this symmetric without touching the LLM classification prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.infrastructure.logger import get_logger
from src.news.classifier import NewsClassification

logger = get_logger(__name__)

_MIN_CONFIDENCE = 0.65
_SHORT_ACTIONS = {"AVOID", "HALT"}


@dataclass
class NewsOpportunity:
    symbol: str
    confidence_boost: float
    event_type: str
    reasoning: str
    affected_symbols: list[str]
    direction: str = "LONG"


class OpportunityScanner:
    """Scans a NewsClassification for actionable NEWS_DRIVEN opportunities."""

    def scan(self, nc: NewsClassification) -> list[NewsOpportunity]:
        if nc.confidence < _MIN_CONFIDENCE or nc.magnitude != "HIGH":
            return []

        if nc.trading_action == "BOOST" and nc.sentiment == "BULLISH":
            direction = "LONG"
        elif nc.trading_action in _SHORT_ACTIONS and nc.sentiment == "BEARISH":
            direction = "SHORT"
        else:
            return []

        symbols = nc.affected_symbols if nc.affected_symbols else [nc.symbol]
        opps = [
            NewsOpportunity(
                symbol=sym,
                confidence_boost=nc.confidence,
                event_type=nc.event_type,
                reasoning=nc.reasoning,
                affected_symbols=nc.affected_symbols,
                direction=direction,
            )
            for sym in symbols
        ]
        logger.info(
            "news_opportunity count=%d event=%s direction=%s", len(opps), nc.event_type, direction
        )
        return opps
