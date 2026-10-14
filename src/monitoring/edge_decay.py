"""
Quarterly Edge Decay Audit.

A strategy's statistical edge degrades over time as market participants
adapt and conditions change.  This module measures whether the recent
performance of a strategy has significantly decayed from its historical
baseline.

Two-window comparison:
  baseline_window : older N trades (e.g. trades 40-21)
  recent_window   : most recent M trades (e.g. last 20)

Metrics compared:
  expectancy  E = WR * avg_win_R - LR * avg_loss_R
  win_rate    WR
  profit_factor PF = gross_wins / gross_losses

Decay verdict:
  STABLE     -- recent metrics within tolerance of baseline
  DEGRADED   -- one metric has decayed significantly
  DECAYED    -- expectancy has fallen below minimum viable threshold
  RETIRED    -- strategy should be taken offline for re-evaluation
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DecayVerdict(StrEnum):
    EDGE_HEALTHY = "EDGE_HEALTHY"  # within tolerance of baseline; normal operation
    EDGE_FADING = "EDGE_FADING"  # one metric decayed; reduce sizing, alert
    EDGE_GONE = "EDGE_GONE"  # expectancy below viable floor; paper-only mode
    RETIRED = "RETIRED"  # expectancy negative; retire for re-validation


_MIN_VIABLE_EXPECTANCY = 0.10  # 0.10R minimum expectancy to remain active
_DECAY_TOLERANCE = 0.25  # 25% relative decay triggers DEGRADED
_RECENT_WINDOW = 20
_BASELINE_WINDOW = 20


@dataclass
class TradeResult:
    pnl_r: float  # trade result in R units (positive = win, negative = loss)


@dataclass
class StrategyMetrics:
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    expectancy: float
    profit_factor: float
    trade_count: int


@dataclass
class EdgeDecayReport:
    strategy_name: str
    verdict: DecayVerdict
    baseline: StrategyMetrics
    recent: StrategyMetrics
    expectancy_decay_pct: float  # (baseline_E - recent_E) / baseline_E
    win_rate_decay_pct: float
    reason: str


@dataclass
class QuarterlyDecayState:
    """Caller-maintained state for 2-consecutive-quarter EDGE_GONE tracking."""

    strategy: str
    consecutive_edge_gone_quarters: int  # incremented each quarter verdict is EDGE_GONE


def _compute_metrics(trades: list[TradeResult]) -> StrategyMetrics:
    if not trades:
        return StrategyMetrics(0, 0, 0, 0, 0, 0)
    n = len(trades)
    wins = [t.pnl_r for t in trades if t.pnl_r > 0]
    losses = [t.pnl_r for t in trades if t.pnl_r <= 0]
    wr = len(wins) / n
    lr = 1 - wr
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    expectancy = wr * avg_win - lr * avg_loss
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    return StrategyMetrics(
        win_rate=round(wr, 4),
        avg_win_r=round(avg_win, 4),
        avg_loss_r=round(avg_loss, 4),
        expectancy=round(expectancy, 4),
        profit_factor=round(pf, 4),
        trade_count=n,
    )


def _decay_pct(baseline: float, recent: float) -> float:
    """Relative decay; positive means recent is worse."""
    if baseline == 0:
        return 0.0
    return (baseline - recent) / abs(baseline)


def audit_edge_decay(
    strategy_name: str,
    all_trades: list[TradeResult],
    recent_window: int = _RECENT_WINDOW,
    baseline_window: int = _BASELINE_WINDOW,
    backtest_expectancy: float | None = None,
) -> EdgeDecayReport:
    """
    Compare recent vs baseline performance to detect edge decay.

    Args:
        strategy_name: Strategy identifier (for the report).
        all_trades: All historical trade results, oldest first.
        recent_window: Number of most-recent trades to treat as "recent".
        baseline_window: Number of trades before that as "baseline".
        backtest_expectancy: Original backtest expectancy (R). When provided,
            uses 80%/50% thresholds relative to this value (spec §8.16).
            When None, falls back to 25% relative-decay comparison.

    Returns:
        EdgeDecayReport with verdict and metric breakdown.
    """
    total_needed = recent_window + baseline_window
    if len(all_trades) < total_needed:
        baseline_trades = all_trades[: max(0, len(all_trades) - recent_window)]
        recent_trades = all_trades[-recent_window:]
    else:
        recent_trades = all_trades[-recent_window:]
        baseline_trades = all_trades[-(recent_window + baseline_window) : -recent_window]

    baseline = _compute_metrics(baseline_trades)
    recent = _compute_metrics(recent_trades)

    e_decay = _decay_pct(baseline.expectancy, recent.expectancy)
    wr_decay = _decay_pct(baseline.win_rate, recent.win_rate)

    if recent.expectancy < 0:
        verdict = DecayVerdict.RETIRED
        reason = (
            f"Recent expectancy {recent.expectancy:.3f}R is negative -- "
            "strategy is net-negative; retire for re-validation"
        )
    elif backtest_expectancy is not None and backtest_expectancy > 0:
        # Use spec §8.16 backtest-relative thresholds: 80% = HEALTHY, 50-80% = FADING, <50% = GONE
        ratio = recent.expectancy / backtest_expectancy
        if ratio >= 0.80:
            verdict = DecayVerdict.EDGE_HEALTHY
            reason = (
                f"Edge healthy. Recent E={recent.expectancy:.3f}R is "
                f"{ratio:.0%} of backtest E={backtest_expectancy:.3f}R"
            )
        elif ratio >= 0.50:
            verdict = DecayVerdict.EDGE_FADING
            reason = (
                f"Edge fading. Recent E={recent.expectancy:.3f}R is "
                f"{ratio:.0%} of backtest E={backtest_expectancy:.3f}R — reduce sizing"
            )
        else:
            verdict = DecayVerdict.EDGE_GONE
            reason = (
                f"Edge gone. Recent E={recent.expectancy:.3f}R is only "
                f"{ratio:.0%} of backtest E={backtest_expectancy:.3f}R — paper only"
            )
    elif recent.expectancy < _MIN_VIABLE_EXPECTANCY:
        verdict = DecayVerdict.EDGE_GONE
        reason = (
            f"Recent expectancy {recent.expectancy:.3f}R < minimum viable "
            f"{_MIN_VIABLE_EXPECTANCY:.2f}R -- paper only"
        )
    elif e_decay > _DECAY_TOLERANCE or wr_decay > _DECAY_TOLERANCE:
        verdict = DecayVerdict.EDGE_FADING
        reason = (
            f"Expectancy decayed {e_decay:.0%} (baseline {baseline.expectancy:.3f}R "
            f"-> recent {recent.expectancy:.3f}R). Reduce sizing."
        )
    else:
        verdict = DecayVerdict.EDGE_HEALTHY
        reason = (
            f"Edge healthy. Recent E={recent.expectancy:.3f}R vs "
            f"baseline E={baseline.expectancy:.3f}R"
        )

    return EdgeDecayReport(
        strategy_name=strategy_name,
        verdict=verdict,
        baseline=baseline,
        recent=recent,
        expectancy_decay_pct=round(e_decay, 4),
        win_rate_decay_pct=round(wr_decay, 4),
        reason=reason,
    )
