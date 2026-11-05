"""
Expectancy Engine â€” rolling 20-trade E per strategy (spec Â§8.4).

E = (WR Ã— avg_win_R) - (LR Ã— avg_loss_R)

Actions:
  Win rate over the rolling window <= 10% (i.e. the strategy is failing ~90%+ of
    the time), with enough trades to trust the read â†’ PAUSED + STRATEGY_DECAY
    incident. (2026-08-21, explicit user decision: learning informs/adjusts —
    calibration thresholds, causal context, attribution — it does not hard-block
    a strategy on a milder signal like a losing streak. Only a near-total failure
    rate should stop it from trading. This intentionally supersedes the original
    spec's "10 consecutive negative-E windows" trigger, which fired far earlier.)
  Current 20-trade E < 50% of all-time E   â†’ WARNING (informational only, doesn't pause)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ROLLING_WINDOW = 20
_PAUSE_WIN_RATE_MAX = 0.10
_PAUSE_MIN_SAMPLE = 10
_DECAY_RATIO = 0.50


class ExpectancyStatus(StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    PAUSED = "PAUSED"


@dataclass
class ExpectancyResult:
    strategy: str
    current_e: float
    all_time_e: float
    status: ExpectancyStatus
    consecutive_negative: int
    trade_count: int
    should_pause: bool
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0


class ExpectancyEngine:
    """
    Tracks rolling expectancy per strategy and signals decay.

    Usage:
        engine = ExpectancyEngine()
        engine.record("VCP", pnl_r=1.5)
        result = engine.evaluate("VCP")
    """

    def __init__(self) -> None:
        self._trades: dict[str, list[float]] = defaultdict(list)

    def record(self, strategy: str, pnl_r: float) -> None:
        self._trades[strategy].append(pnl_r)

    def evaluate(self, strategy: str) -> ExpectancyResult:
        trades = self._trades.get(strategy, [])
        if not trades:
            return ExpectancyResult(
                strategy=strategy,
                current_e=0.0,
                all_time_e=0.0,
                status=ExpectancyStatus.HEALTHY,
                consecutive_negative=0,
                trade_count=0,
                should_pause=False,
            )

        window = trades[-_ROLLING_WINDOW:]
        current_e = _expectancy(window)
        all_time_e = _expectancy(trades)
        consecutive_neg = _consecutive_negative_e(trades, _ROLLING_WINDOW)
        window_win_rate = len([r for r in window if r > 0]) / len(window)
        should_pause = len(window) >= _PAUSE_MIN_SAMPLE and window_win_rate <= _PAUSE_WIN_RATE_MAX
        wins = [r for r in trades if r > 0]
        losses = [abs(r) for r in trades if r <= 0]
        win_rate = len(wins) / len(trades)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        if should_pause:
            status = ExpectancyStatus.PAUSED
            logger.warning(
                "strategy=%s PAUSED window_win_rate=%.1f%% (fails ~%.0f%% of the time)",
                strategy,
                window_win_rate * 100,
                (1 - window_win_rate) * 100,
            )
        elif all_time_e > 0 and current_e < all_time_e * _DECAY_RATIO:
            status = ExpectancyStatus.WARNING
            logger.warning(
                "strategy=%s DECAY_WARNING current_e=%.3f all_time_e=%.3f",
                strategy,
                current_e,
                all_time_e,
            )
        else:
            status = ExpectancyStatus.HEALTHY

        return ExpectancyResult(
            strategy=strategy,
            current_e=round(current_e, 4),
            all_time_e=round(all_time_e, 4),
            status=status,
            consecutive_negative=consecutive_neg,
            trade_count=len(trades),
            should_pause=should_pause,
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 4),
            avg_loss=round(avg_loss, 4),
        )


def _expectancy(trades: list[float]) -> float:
    if not trades:
        return 0.0
    n = len(trades)
    wins = [r for r in trades if r > 0]
    losses = [abs(r) for r in trades if r <= 0]
    wr = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return (wr * avg_win) - ((1 - wr) * avg_loss)


def _consecutive_negative_e(trades: list[float], window: int) -> int:
    count = 0
    for end in range(len(trades), 0, -1):
        w = trades[max(0, end - window) : end]
        if len(w) < 2:
            break
        if _expectancy(w) < 0:
            count += 1
        else:
            break
    return count
