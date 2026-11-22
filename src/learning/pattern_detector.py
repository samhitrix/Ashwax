"""
Layer 3 â€” Pattern Detector: regime-conditional pattern recognition.

Example: "VCP_BREAKOUT underperforms in HIGH_VOL regime"

Confidence tiers (spec Â§9):
  WEAK   â€” 3-4 observations
  MEDIUM â€” 5-6 observations
  STRONG â€” 7+ observations
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from src.infrastructure.logger import get_logger
from src.learning.trade_autopsy import TradeAutopsy

logger = get_logger(__name__)

_WEAK_THRESHOLD = 3
_MEDIUM_THRESHOLD = 5
_STRONG_THRESHOLD = 7


class PatternConfidence(StrEnum):
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


@dataclass
class DetectedPattern:
    strategy: str
    regime: str
    win_rate: float
    observation_count: int
    confidence: PatternConfidence
    description: str
    recommendation: str  # AVOID | REDUCE_SIZE | NORMAL


class PatternDetector:
    """
    Detects regime-conditional patterns from accumulated TradeAutopsy records.

    Usage:
        detector = PatternDetector()
        detector.add(autopsy)
        patterns = detector.detect()
    """

    def __init__(self, pattern_min_sample_size: int = 15) -> None:
        self._obs: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._pattern_min_sample_size = pattern_min_sample_size

    def add(self, autopsy: TradeAutopsy) -> None:
        self._obs[(autopsy.strategy, autopsy.regime_at_entry)].append(autopsy.pnl_r)

    def detect(self) -> list[DetectedPattern]:
        patterns: list[DetectedPattern] = []
        for (strategy, regime), pnl_list in self._obs.items():
            n = len(pnl_list)
            if n < _WEAK_THRESHOLD:
                continue
            wr = sum(1 for r in pnl_list if r > 0) / n
            desc, rec = _describe(strategy, regime, wr)
            patterns.append(
                DetectedPattern(
                    strategy=strategy,
                    regime=regime,
                    win_rate=round(wr, 4),
                    observation_count=n,
                    confidence=_confidence_tier(n),
                    description=desc,
                    recommendation=rec,
                )
            )
        return patterns

    def pattern_for(self, strategy: str, regime: str) -> DetectedPattern | None:
        for p in self.detect():
            if p.strategy == strategy and p.regime == regime:
                return p
        return None


def _confidence_tier(n: int) -> PatternConfidence:
    if n >= _STRONG_THRESHOLD:
        return PatternConfidence.STRONG
    if n >= _MEDIUM_THRESHOLD:
        return PatternConfidence.MEDIUM
    return PatternConfidence.WEAK


def _describe(strategy: str, regime: str, wr: float) -> tuple[str, str]:
    if wr < 0.35:
        return f"{strategy} significantly underperforms in {regime} (WR={wr:.0%})", "AVOID"
    if wr < 0.50:
        return f"{strategy} underperforms in {regime} (WR={wr:.0%})", "REDUCE_SIZE"
    return f"{strategy} performs well in {regime} (WR={wr:.0%})", "NORMAL"
