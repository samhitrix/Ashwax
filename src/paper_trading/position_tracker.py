"""
PaperPositionTracker — tracks open paper positions with real LTP.

P&L = (current_real_ltp - simulated_fill_price) × quantity — always uses real LTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


@dataclass
class PaperPosition:
    agent_trade_id: str
    symbol: str
    direction: str
    quantity: int
    fill_price: float
    initial_sl: float
    current_sl: float
    initial_target: float
    current_target: float
    strategy_name: str = ""
    entry_timestamp: datetime = field(default_factory=lambda: datetime.now(_IST))
    current_ltp: float = 0.0
    unrealized_pnl: float = 0.0

    def update_ltp(self, ltp: float) -> None:
        self.current_ltp = ltp
        mult = 1 if self.direction.upper() == "BUY" else -1
        self.unrealized_pnl = (ltp - self.fill_price) * self.quantity * mult


class PaperPositionTracker:
    """In-memory store for open paper positions."""

    def __init__(self) -> None:
        self._positions: dict[str, PaperPosition] = {}

    def open(self, pos: PaperPosition) -> None:
        self._positions[pos.agent_trade_id] = pos

    def close(self, agent_trade_id: str) -> PaperPosition | None:
        return self._positions.pop(agent_trade_id, None)

    def get(self, agent_trade_id: str) -> PaperPosition | None:
        return self._positions.get(agent_trade_id)

    def get_all(self) -> list[PaperPosition]:
        return list(self._positions.values())

    def update_ltp(self, symbol: str, ltp: float) -> None:
        for pos in self._positions.values():
            if pos.symbol == symbol:
                pos.update_ltp(ltp)

    def total_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values())

    def symbols(self) -> list[str]:
        return list({p.symbol for p in self._positions.values()})
