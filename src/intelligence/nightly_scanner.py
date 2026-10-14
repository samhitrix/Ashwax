"""Nightly Setup Scanner — pre-market swing setup scan."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("intelligence.nightly_scanner")


@dataclass
class NightlySetup:
    symbol: str
    strategy_name: str
    quality_score: float
    key_level: float
    sl_level: float
    target_level: float
    notes: str = ""
    context: dict[str, Any] = field(default_factory=dict)


class NightlySetupScanner:
    def __init__(self, strategies: list[Any] | None = None) -> None:
        self._strategies = strategies or []

    async def scan(self, universe: list[str], daily_data: dict[str, Any]) -> list[NightlySetup]:
        setups: list[NightlySetup] = []
        for symbol in universe:
            data = daily_data.get(symbol, {})
            if not data:
                continue
            for strategy in self._strategies:
                try:
                    if not hasattr(strategy, "evaluate_daily"):
                        continue
                    result = strategy.evaluate_daily(symbol, data)
                    if result and result.get("quality_score", 0) >= 65:
                        setups.append(
                            NightlySetup(
                                symbol=symbol,
                                strategy_name=getattr(strategy, "name", "unknown"),
                                quality_score=result["quality_score"],
                                key_level=result.get("key_level", 0),
                                sl_level=result.get("sl_level", 0),
                                target_level=result.get("target_level", 0),
                            )
                        )
                except Exception as exc:
                    _LOG.error("scan_error symbol=%s err=%s", symbol, exc)
        return sorted(setups, key=lambda s: s.quality_score, reverse=True)
