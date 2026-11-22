"""Layer 5A — Universe Adapter: narrows universe to best-fit stocks per strategy.

Tracks paper and live outcomes SEPARATELY (a "mode" dimension) — same
rationale as strategy_selector.py: a strategy's simulated paper wins must not
shape what the LIVE version of that strategy is allowed to trade, and vice
versa.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)  # "src.learning.universe_adapter" -> learning.log


@dataclass
class UniverseAdaptation:
    strategy_name: str
    preferred_sectors: list[str] = field(default_factory=list)
    min_rs_rank: float = 60.0


class UniverseAdapter:
    def __init__(self) -> None:
        # {mode: {name: UniverseAdaptation}}
        self._data: dict[str, dict[str, UniverseAdaptation]] = {"paper": {}, "live": {}}

    def update(self, name: str, outcomes: list[dict[str, Any]], mode: str = "live") -> None:
        wins = [t for t in outcomes if t.get("pnl_r", 0) > 0]
        if len(wins) < 10:
            return
        from collections import Counter

        top = [
            s
            for s, _ in Counter(t.get("sector", "") for t in wins if t.get("sector")).most_common(5)
        ]
        self._data.setdefault(mode, {})[name] = UniverseAdaptation(name, top)

    def filter_universe(
        self, name: str, symbols: list[str], meta: dict[str, dict[str, Any]], mode: str = "live"
    ) -> list[str]:
        a = self._data.get(mode, {}).get(name)
        if not a:
            return symbols
        return [
            s
            for s in symbols
            if (not a.preferred_sectors or meta.get(s, {}).get("sector") in a.preferred_sectors)
            and float(meta.get(s, {}).get("rs_rank", 0)) >= a.min_rs_rank
        ]
