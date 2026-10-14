"""
StrategyHealthMonitor â€” CRM Gate 6.

Per strategy, rolling 20-trade scoring:

  Score = 100
    - (baseline_WR - actual_WR) Ã— 200    (capped at -40)
    - (baseline_avg_R - actual_avg_R) Ã— 20 (capped at -20)
    - consecutive_losses Ã— 5              (capped at -20)
    - drawdown_pct Ã— 2                   (capped at -20)

Tier â†’ sizing multiplier:
  HEALTHY   80-100   1.00Ã—
  CAUTIOUS  60-79    0.75Ã—
  DEGRADED  40-59    0.50Ã—
  STRESSED  20-39    0.25Ã—
  SUSPENDED  0-19   0.00Ã— (reject)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_ROLLING_WINDOW = 20


class HealthTier(StrEnum):
    HEALTHY = "HEALTHY"
    CAUTIOUS = "CAUTIOUS"
    DEGRADED = "DEGRADED"
    STRESSED = "STRESSED"
    SUSPENDED = "SUSPENDED"


@dataclass
class StrategyHealth:
    strategy_name: str
    score: float
    tier: HealthTier
    sizing_multiplier: float
    win_rate: float
    avg_r: float
    consecutive_losses: int
    trade_count: int


@dataclass
class TradeRecord:
    """Minimal trade representation needed for health scoring."""

    pnl_r: float  # positive = win, negative = loss


def _tier_from_score(score: float) -> HealthTier:
    if score >= 80:
        return HealthTier.HEALTHY
    if score >= 60:
        return HealthTier.CAUTIOUS
    if score >= 40:
        return HealthTier.DEGRADED
    if score >= 20:
        return HealthTier.STRESSED
    return HealthTier.SUSPENDED


def _sizing_from_tier(tier: HealthTier) -> float:
    return {
        HealthTier.HEALTHY: 1.00,
        HealthTier.CAUTIOUS: 0.75,
        HealthTier.DEGRADED: 0.50,
        HealthTier.STRESSED: 0.25,
        HealthTier.SUSPENDED: 0.00,
    }[tier]


class StrategyHealthMonitor:
    """
    Scores strategy health from a rolling trade window.

    evaluation_window_trades, degraded_win_rate_threshold, and suspended_win_rate_threshold
    are loaded from intelligence.json strategy_health.* at construction time.

    Usage:
        monitor = StrategyHealthMonitor(evaluation_window_trades=20)
        health = monitor.score("vcp_breakout", trades, baseline_wr=0.55, baseline_avg_r=1.8)
    """

    def __init__(
        self,
        evaluation_window_trades: int = _ROLLING_WINDOW,
        degraded_win_rate_threshold: float = 0.40,
        suspended_win_rate_threshold: float = 0.30,
        recovery_window_trades: int = 10,
        min_trades_for_suspension: int = 10,
    ) -> None:
        # Below this many closed trades the win-rate/avg-R/drawdown terms are noise
        # (3 trades = 33% WR by chance), so STRESSED/SUSPENDED are capped at DEGRADED.
        self._min_trades_for_suspension = min_trades_for_suspension
        self._rolling_window = evaluation_window_trades
        self._degraded_win_rate_threshold = degraded_win_rate_threshold
        self._suspended_win_rate_threshold = suspended_win_rate_threshold
        self._recovery_window_trades = recovery_window_trades

    def score(
        self,
        strategy_name: str,
        trades: list[TradeRecord],
        baseline_wr: float = 0.50,
        baseline_avg_r: float = 1.5,
    ) -> StrategyHealth:
        window = trades[-self._rolling_window :]
        n = len(window)

        if n == 0:
            return StrategyHealth(
                strategy_name=strategy_name,
                score=100.0,
                tier=HealthTier.HEALTHY,
                sizing_multiplier=1.0,
                win_rate=0.0,
                avg_r=0.0,
                consecutive_losses=0,
                trade_count=0,
            )

        wins = [t for t in window if t.pnl_r > 0]
        actual_wr = len(wins) / n
        actual_avg_r = sum(t.pnl_r for t in window) / n

        consec = 0
        for t in reversed(window):
            if t.pnl_r <= 0:
                consec += 1
            else:
                break

        equity = 0.0
        peak = 0.0
        max_dd_r = 0.0
        for t in window:
            equity += t.pnl_r
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd_r:
                max_dd_r = dd
        drawdown_pct = (max_dd_r / max(peak, 1.0)) * 100

        wr_penalty = min(40.0, max(0.0, (baseline_wr - actual_wr) * 200))
        r_penalty = min(20.0, max(0.0, (baseline_avg_r - actual_avg_r) * 20))
        consec_penalty = min(20.0, consec * 5.0)
        dd_penalty = min(20.0, drawdown_pct * 2)

        score = max(0.0, min(100.0, 100.0 - wr_penalty - r_penalty - consec_penalty - dd_penalty))
        tier = _tier_from_score(score)

        if n < self._min_trades_for_suspension and tier in (
            HealthTier.STRESSED,
            HealthTier.SUSPENDED,
        ):
            logger.warning(
                "strategy=%s tier=%s->%s score=%.1f live_trades=%d < min_trades_for_suspension=%d "
                "— sample too small to suspend, capped at DEGRADED",
                strategy_name,
                tier,
                HealthTier.DEGRADED,
                score,
                n,
                self._min_trades_for_suspension,
            )
            tier = HealthTier.DEGRADED

        if tier in (HealthTier.STRESSED, HealthTier.SUSPENDED):
            logger.warning(
                "strategy=%s tier=%s score=%.1f wr=%.0f%% consec_losses=%d live_trades=%d "
                "avg_r=%.2f penalties(wr=%.0f r=%.0f consec=%.0f dd=%.0f) baseline_wr=%.2f "
                "baseline_avg_r=%.2f%s",
                strategy_name,
                tier,
                score,
                actual_wr * 100,
                consec,
                n,
                actual_avg_r,
                wr_penalty,
                r_penalty,
                consec_penalty,
                dd_penalty,
                baseline_wr,
                baseline_avg_r,
                (
                    " — auto-suspended by StrategyHealthMonitor (score<20); CRM G6 blocks new entries"
                    if tier == HealthTier.SUSPENDED
                    else ""
                ),
            )

        return StrategyHealth(
            strategy_name=strategy_name,
            score=round(score, 1),
            tier=tier,
            sizing_multiplier=_sizing_from_tier(tier),
            win_rate=actual_wr,
            avg_r=round(actual_avg_r, 3),
            consecutive_losses=consec,
            trade_count=n,
        )
