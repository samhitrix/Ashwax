"""A/B Tester — 50/50 parameter split, 20 trades per leg, winner deployed."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("strategies.ab_tester")


@dataclass
class ABTest:
    strategy_name: str
    params_a: dict[str, Any]
    params_b: dict[str, Any]
    trades_a: list[float] = field(default_factory=list)
    trades_b: list[float] = field(default_factory=list)
    winner: str | None = None


@dataclass
class ABResult:
    winner: str
    winning_params: dict[str, Any]
    wr_a: float
    wr_b: float
    expectancy_a: float
    expectancy_b: float


class ABTester:
    TRADES_PER_LEG = 20

    def __init__(self) -> None:
        self._tests: dict[str, ABTest] = {}

    def start(self, name: str, current: dict[str, Any], evolved: dict[str, Any]) -> None:
        self._tests[name] = ABTest(name, current, evolved)

    def record(self, name: str, leg: str, pnl_r: float) -> None:
        t = self._tests.get(name)
        if t and not t.winner:
            (t.trades_a if leg == "A" else t.trades_b).append(pnl_r)

    def check(self, name: str) -> ABResult | None:
        t = self._tests.get(name)
        if not t or len(t.trades_a) < self.TRADES_PER_LEG or len(t.trades_b) < self.TRADES_PER_LEG:
            return None

        def s(xs: list[float]) -> tuple[float, float]:
            return sum(1 for x in xs if x > 0) / len(xs), sum(xs) / len(xs)

        wr_a, exp_a = s(t.trades_a)
        wr_b, exp_b = s(t.trades_b)
        winner = "B" if exp_b > exp_a else "A"
        params = t.params_b if winner == "B" else t.params_a
        del self._tests[name]
        return ABResult(winner, params, wr_a, wr_b, exp_a, exp_b)
