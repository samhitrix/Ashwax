"""
PatientWatcher — anti-FOMO patience tracker (spec §10).

Tracks signals evaluated vs trades taken.
Goldilocks zone: 5-20% trade rate.
  >20% → quality filter too loose → raise min_score
  <5%  → market poor (correct) OR filter too tight (check backtest)
Days with 0 qualifying setups → logged as CORRECT_BEHAVIOR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_GOLDILOCKS_LOW = 0.05
_GOLDILOCKS_HIGH = 0.20
_MIN_SAMPLE = 10  # minimum signals before acting on trade rate


@dataclass
class PatientWatchResult:
    signals_seen: int
    trades_taken: int
    trade_rate: float
    recommendation: str  # OK | RAISE_QUALITY_FLOOR | REVIEW_FILTER
    days_correct_patience: int
    auto_raise_score: float  # additional min_score raise (0 if OK)


class PatientWatcher:
    """
    Tracks signal/trade ratio and flags quality-filter drift.

    Usage:
        watcher = PatientWatcher()
        watcher.record_signal("RELIANCE", score=72.0)
        watcher.record_trade("RELIANCE")
        watcher.record_day_end(had_qualifying_setups=False)
        result = watcher.evaluate()
    """

    def __init__(self, min_score: float = 65.0) -> None:
        self._min_score = min_score
        self._signals: list[tuple[str, float]] = []  # (symbol, score)
        self._trades: set[str] = set()  # symbols actually traded
        self._day_log: dict[date, bool] = {}  # date → had_qualifying_setups
        self._auto_raise: float = 0.0

    def record_signal(self, symbol: str, score: float) -> None:
        self._signals.append((symbol, score))

    def record_trade(self, symbol: str) -> None:
        self._trades.add(symbol)

    def record_day_end(self, today: date | None = None, had_qualifying_setups: bool = True) -> None:
        day = today or date.today()
        self._day_log[day] = had_qualifying_setups
        if not had_qualifying_setups:
            logger.info("patient_watcher CORRECT_BEHAVIOR date=%s no_qualifying_setups", day)

    def evaluate(self) -> PatientWatchResult:
        n_signals = len(self._signals)
        n_trades = len(self._trades)
        days_correct = sum(1 for v in self._day_log.values() if not v)

        if n_signals < _MIN_SAMPLE:
            return PatientWatchResult(
                signals_seen=n_signals,
                trades_taken=n_trades,
                trade_rate=0.0,
                recommendation="OK",
                days_correct_patience=days_correct,
                auto_raise_score=0.0,
            )

        rate = n_trades / n_signals

        if rate > _GOLDILOCKS_HIGH:
            # Filter too loose — auto-raise min score by 5 points
            self._auto_raise = min(self._auto_raise + 5.0, 20.0)
            rec = "RAISE_QUALITY_FLOOR"
            logger.warning(
                "patient_watcher trade_rate=%.0f%% > goldilocks — raising quality floor by %.0f",
                rate * 100,
                self._auto_raise,
            )
        elif rate < _GOLDILOCKS_LOW:
            rec = "REVIEW_FILTER"
            logger.info(
                "patient_watcher trade_rate=%.0f%% < goldilocks — market poor or filter too tight",
                rate * 100,
            )
        else:
            rec = "OK"

        return PatientWatchResult(
            signals_seen=n_signals,
            trades_taken=n_trades,
            trade_rate=round(rate, 4),
            recommendation=rec,
            days_correct_patience=days_correct,
            auto_raise_score=self._auto_raise,
        )

    @property
    def effective_min_score(self) -> float:
        return self._min_score + self._auto_raise
