"""
Layer 1 â€” Trade Autopsy: symmetric post-trade analysis for wins AND losses.

Produces a TradeAutopsy consumed by Attribution Engine (Layer 2)
and Causal Trade Memory (Layer 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)


class TradeOutcome(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    BREAKEVEN = "BREAKEVEN"


class FailureMode(StrEnum):
    REGIME_MISMATCH = "REGIME_MISMATCH"
    TIMING_FAILURE = "TIMING_FAILURE"
    SIGNAL_QUALITY = "SIGNAL_QUALITY"
    NEWS_ADVERSE = "NEWS_ADVERSE"
    UNKNOWN = "UNKNOWN"


class WinDriver(StrEnum):
    REGIME_MATCH = "WIN_REGIME_MATCH"
    TIMING_OPTIMAL = "WIN_TIMING_OPTIMAL"
    SIGNAL_QUALITY_HIGH = "WIN_SIGNAL_QUALITY_HIGH"
    MARKET_TAILWIND = "WIN_MARKET_TAILWIND"


@dataclass
class TradeAutopsy:
    trade_id: str
    strategy: str
    outcome: TradeOutcome
    pnl_r: float
    regime_at_entry: str
    primary_failure_mode: FailureMode | None
    was_signal_valid: bool
    failure_was_environmental: bool
    primary_win_driver: WinDriver | None
    was_win_skill_based: bool
    sl_placement_quality: str  # TIGHT | GOOD | WIDE
    entry_quality: str  # OPTIMAL | ACCEPTABLE | POOR
    notes: str = ""


class TradeAutopsyEngine:
    """
    Produces a TradeAutopsy from raw trade data.

    autopsy_enabled is loaded from intelligence.json learning.autopsy_enabled.

    Usage:
        autopsy = TradeAutopsyEngine().analyse(trade_id, strategy, pnl_r, regime, ...)
    """

    def __init__(self, autopsy_enabled: bool = True) -> None:
        self._autopsy_enabled = autopsy_enabled

    def analyse(
        self,
        trade_id: str,
        strategy: str,
        pnl_r: float,
        regime_at_entry: str,
        entry_vs_pivot_pct: float = 0.0,
        sl_atr_ratio: float = 1.0,
        had_adverse_news: bool = False,
        was_regime_unknown: bool = False,
    ) -> TradeAutopsy:
        outcome = _outcome(pnl_r)
        failure_mode, failure_env = _diagnose_failure(
            outcome, regime_at_entry, entry_vs_pivot_pct, had_adverse_news, was_regime_unknown
        )
        win_driver, skill_based = _diagnose_win(outcome, regime_at_entry, entry_vs_pivot_pct)

        return TradeAutopsy(
            trade_id=trade_id,
            strategy=strategy,
            outcome=outcome,
            pnl_r=pnl_r,
            regime_at_entry=regime_at_entry,
            primary_failure_mode=failure_mode,
            was_signal_valid=outcome == TradeOutcome.WIN or not failure_env,
            failure_was_environmental=failure_env,
            primary_win_driver=win_driver,
            was_win_skill_based=skill_based,
            sl_placement_quality=_sl_quality(sl_atr_ratio),
            entry_quality=_entry_quality(entry_vs_pivot_pct),
        )


def _outcome(pnl_r: float) -> TradeOutcome:
    if pnl_r > 0.05:
        return TradeOutcome.WIN
    if pnl_r < -0.05:
        return TradeOutcome.LOSS
    return TradeOutcome.BREAKEVEN


def _diagnose_failure(
    outcome: TradeOutcome,
    regime: str,
    entry_vs_pivot: float,
    adverse_news: bool,
    unknown_regime: bool,
) -> tuple[FailureMode | None, bool]:
    if outcome == TradeOutcome.WIN:
        return None, False
    if adverse_news:
        return FailureMode.NEWS_ADVERSE, True
    if unknown_regime or regime in ("BEAR", "STRONG_BEAR"):
        return FailureMode.REGIME_MISMATCH, True
    if entry_vs_pivot > 0.03:
        return FailureMode.TIMING_FAILURE, False
    return FailureMode.SIGNAL_QUALITY, False


def _diagnose_win(
    outcome: TradeOutcome,
    regime: str,
    entry_vs_pivot: float,
) -> tuple[WinDriver | None, bool]:
    if outcome != TradeOutcome.WIN:
        return None, False
    if regime in ("STRONG_BULL", "BULL") and entry_vs_pivot <= 0.01:
        return WinDriver.TIMING_OPTIMAL, True
    if regime in ("STRONG_BULL", "BULL"):
        return WinDriver.REGIME_MATCH, True
    if regime == "NEUTRAL":
        return WinDriver.SIGNAL_QUALITY_HIGH, True
    return WinDriver.MARKET_TAILWIND, False


def _sl_quality(sl_atr_ratio: float) -> str:
    if sl_atr_ratio < 0.5:
        return "TIGHT"
    if sl_atr_ratio > 2.0:
        return "WIDE"
    return "GOOD"


def _entry_quality(entry_vs_pivot: float) -> str:
    if entry_vs_pivot <= 0.005:
        return "OPTIMAL"
    if entry_vs_pivot <= 0.02:
        return "ACCEPTABLE"
    return "POOR"
