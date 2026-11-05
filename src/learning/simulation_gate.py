"""Layer 5 — Simulation Gate: walk-forward validation before promotion."""

from __future__ import annotations

import logging
from typing import Any

from src.strategies.backtest.walk_forward import WalkForwardResult, WalkForwardValidator

_LOG = logging.getLogger(__name__)  # "src.learning.simulation_gate" -> learning.log


class SimulationGate:
    def __init__(self) -> None:
        self._validator = WalkForwardValidator()

    def evaluate(
        self,
        strategy_name: str,
        _params: dict[str, Any],
        train: dict[str, Any],
        test: dict[str, Any],
    ) -> WalkForwardResult:
        result = self._validator.validate(strategy_name, train, test)
        _LOG.info("gate strategy=%s passed=%s", strategy_name, result.passed)
        return result
