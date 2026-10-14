"""Layer 4A — Strategy Selector: regime-aware signal prioritization.

Tracks paper and live expectancy SEPARATELY (a "mode" dimension alongside
regime) — a strategy's simulated paper performance must never blend into or
substitute for its real-money live performance, and vice versa, mirroring
this codebase's existing paper/live separation convention (see
orchestrator.py's _fetch_closed_trades_from_db docstring: "mixing paper
simulated losses into either would let a paper strategy raise the quality bar
or hard-pause a strategy's real trading, and vice versa" — the same principle
applies to ranking priority).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)  # "src.learning.strategy_selector" -> learning.log


@dataclass
class StrategyPriority:
    strategy_name: str
    regime: str
    expectancy_r: float
    weight: float


class StrategySelector:
    def __init__(self) -> None:
        # {mode: {regime: {name: expectancy_r}}}
        self._data: dict[str, dict[str, dict[str, float]]] = {"paper": {}, "live": {}}

    def update(self, name: str, regime: str, expectancy_r: float, mode: str = "live") -> None:
        self._data.setdefault(mode, {}).setdefault(regime, {})[name] = expectancy_r

    def get_priorities(
        self, regime: str, available: list[str], mode: str = "live"
    ) -> list[StrategyPriority]:
        rd = self._data.get(mode, {}).get(regime, {})
        total = sum(max(0, v) for v in rd.values()) or 1.0
        return sorted(
            [
                StrategyPriority(n, regime, rd.get(n, 0.3), max(0, rd.get(n, 0.3)) / total)
                for n in available
            ],
            key=lambda p: p.expectancy_r,
            reverse=True,
        )
