"""Walk-forward backtest validation — Layer 5 SimulationGate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class WalkForwardResult:
    strategy_name: str
    train_win_rate: float
    test_win_rate: float
    train_profit_factor: float
    test_profit_factor: float
    max_drawdown: float
    expectancy_r: float
    passed: bool
    reason: str


class WalkForwardValidator:
    MIN_WIN_RATE = 0.35
    MIN_PROFIT_FACTOR = 1.3
    MIN_EXPECTANCY_R = 0.20
    MAX_DRAWDOWN = 0.20

    def validate(
        self, strategy_name: str, train: dict[str, Any], test: dict[str, Any]
    ) -> WalkForwardResult:
        wr = float(test.get("win_rate", 0))
        pf = float(test.get("profit_factor", 0))
        exp = float(test.get("expectancy_r", 0))
        mdd = float(test.get("max_drawdown", 1))
        passed = (
            wr >= self.MIN_WIN_RATE
            and pf >= self.MIN_PROFIT_FACTOR
            and exp >= self.MIN_EXPECTANCY_R
            and mdd <= self.MAX_DRAWDOWN
        )
        return WalkForwardResult(
            strategy_name=strategy_name,
            train_win_rate=float(train.get("win_rate", 0)),
            test_win_rate=wr,
            train_profit_factor=float(train.get("profit_factor", 0)),
            test_profit_factor=pf,
            max_drawdown=mdd,
            expectancy_r=exp,
            passed=passed,
            reason="PASS"
            if passed
            else f"FAIL wr={wr:.2f} pf={pf:.2f} exp={exp:.2f} mdd={mdd:.2f}",
        )
