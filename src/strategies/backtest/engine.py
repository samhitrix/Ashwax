"""
BacktestEngine — runs a CompiledStrategy on a DataFrame and decides pass/fail.

Thresholds (from architecture spec):
  min_trades    >= 20
  min_win_rate  >= 0.35
  min_pf        >= 1.30   (profit factor)
  min_expectancy >= 0.20   (in R units)

FAIL → strategy rejected, not registered.
PASS → strategy registered, monitoring starts.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..uss.compiler import BacktestSignal, CompiledStrategy
from ..uss.schema import Direction


@dataclass
class BacktestResult:
    strategy_name: str
    passed: bool
    total_trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    max_drawdown_r: float
    signals: list[BacktestSignal]
    reject_reason: str = ""
    long_trades: int = 0  # trades taken to the long side
    short_trades: int = 0  # trades taken to the short side

    def summary(self) -> str:
        status = "PASS" if self.passed else f"FAIL ({self.reject_reason})"
        return (
            f"{self.strategy_name}: {status} | "
            f"trades={self.total_trades} WR={self.win_rate:.1%} "
            f"PF={self.profit_factor:.2f} E={self.expectancy_r:.2f}R"
        )


_MIN_TRADES = 20
_MIN_WIN_RATE = 0.35
_MIN_PF = 1.30
_MIN_EXPECTANCY = 0.20


class BacktestEngine:
    """Runs CompiledStrategy.generate_backtest_signals and scores results.

    cost_pct: round-trip transaction cost (brokerage + STT + fees + slippage)
    as a percent of trade notional, converted to R per trade via the trade's
    own stop distance. Default 0.0 preserves historical (pre-cost) scoring.
    """

    def __init__(self, cost_pct: float = 0.0) -> None:
        self._cost_pct = cost_pct

    def run(
        self,
        strategy: CompiledStrategy,
        df: pd.DataFrame,
        market_df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        if market_df is not None:
            strategy.set_market_context(market_df)
        signals = strategy.generate_backtest_signals(df)
        return self._score(strategy.spec.meta.name, signals)

    def _score(self, name: str, signals: list[BacktestSignal]) -> BacktestResult:
        # Score on trade LEGS, not positions: a partial-booked position closes in
        # two legs (50% @ partial_r, remaining 50% at its exit), which TradingView
        # and broker statements count as separate trades. Strategies without
        # partial booking emit one full-size leg per position, so their stats are
        # unchanged (leg list == position list).
        legs: list[float] = []
        for s in signals:
            cost_r = self._cost_r(s)
            if s.leg_pnls_r:
                legs.extend(leg - cost_r for leg in s.leg_pnls_r)
            elif s.pnl_r is not None:
                legs.append(s.pnl_r - cost_r)
        n = len(legs)
        long_trades = sum(1 for s in signals if s.direction == Direction.LONG)
        short_trades = sum(1 for s in signals if s.direction == Direction.SHORT)

        if n < _MIN_TRADES:
            return BacktestResult(
                strategy_name=name,
                passed=False,
                total_trades=n,
                win_rate=0.0,
                profit_factor=0.0,
                expectancy_r=0.0,
                avg_win_r=0.0,
                avg_loss_r=0.0,
                max_drawdown_r=0.0,
                signals=signals,
                reject_reason=f"insufficient trades ({n} < {_MIN_TRADES})",
                long_trades=long_trades,
                short_trades=short_trades,
            )
        wins = [r for r in legs if r > 0]
        losses = [r for r in legs if r <= 0]

        win_rate = len(wins) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
        max_dd = self._max_drawdown(legs)

        reject = ""
        if win_rate < _MIN_WIN_RATE:
            reject = f"win_rate={win_rate:.1%} < {_MIN_WIN_RATE:.0%}"
        elif pf < _MIN_PF:
            reject = f"profit_factor={pf:.2f} < {_MIN_PF}"
        elif expectancy < _MIN_EXPECTANCY:
            reject = f"expectancy={expectancy:.2f}R < {_MIN_EXPECTANCY}R"

        return BacktestResult(
            strategy_name=name,
            passed=reject == "",
            total_trades=n,
            win_rate=win_rate,
            profit_factor=pf,
            expectancy_r=expectancy,
            avg_win_r=avg_win,
            avg_loss_r=avg_loss,
            max_drawdown_r=max_dd,
            signals=signals,
            reject_reason=reject,
            long_trades=long_trades,
            short_trades=short_trades,
        )

    def _cost_r(self, s: BacktestSignal) -> float:
        """Round-trip cost of one signal expressed in R units (0 when disabled)."""
        if self._cost_pct <= 0.0 or not s.entry_price:
            return 0.0
        risk_pct = abs(s.entry_price - s.sl) / s.entry_price
        if risk_pct <= 1e-6:
            return 1.0  # degenerate stop: cap the cost at a full R
        return min(self._cost_pct / 100.0 / risk_pct, 1.0)

    @staticmethod
    def _max_drawdown(pnl_r: list[float]) -> float:
        peak = 0.0
        trough = 0.0
        equity = 0.0
        max_dd = 0.0
        for r in pnl_r:
            equity += r
            if equity > peak:
                peak = equity
                trough = equity
            else:
                trough = min(trough, equity)
                dd = peak - trough
                if dd > max_dd:
                    max_dd = dd
        return max_dd
