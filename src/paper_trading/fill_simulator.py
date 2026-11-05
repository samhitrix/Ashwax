"""
Paper fill simulator — simulates order fills using REAL broker LTP.

Per docs/05-order-lifecycle.md §6.5:
  MARKET → fill at LTP + random slippage 0-0.3%
  LIMIT  → fill only when LTP reaches limit price
  SL/SL-M → trigger when LTP hits trigger price

Real LTP always fetched from broker even in paper mode.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

from src.brokers.base import OrderRequest


@dataclass
class PaperFill:
    agent_trade_id: str
    symbol: str
    filled: bool
    fill_price: float
    simulated_slippage_pct: float = 0.0
    reason: str = ""


class PaperFillSimulator:
    """Simulates paper order fills against real broker LTP."""

    DEFAULT_MAX_SLIPPAGE_PCT = 0.003

    def __init__(
        self, broker: BrokerInterface, max_slippage_pct: float = DEFAULT_MAX_SLIPPAGE_PCT
    ) -> None:
        self._broker = broker
        self._max_slippage = max_slippage_pct

    async def try_fill(self, order: OrderRequest, current_ltp: float | None = None) -> PaperFill:
        """Attempt to fill a paper order against current LTP."""
        if current_ltp is None:
            try:
                ltp_map = await self._broker.get_ltp([order.symbol])
                raw = ltp_map.get(order.symbol) or ltp_map.get(f"NSE:{order.symbol}")
                if isinstance(raw, dict):
                    current_ltp = float(raw.get("last_price", 0) or 0)
                elif raw is not None:
                    current_ltp = float(raw)
                else:
                    current_ltp = 0.0
            except Exception as exc:
                return PaperFill(
                    agent_trade_id=order.agent_trade_id,
                    symbol=order.symbol,
                    filled=False,
                    fill_price=0.0,
                    reason=f"ltp_fetch_error:{exc}",
                )

        if not current_ltp:
            return PaperFill(
                agent_trade_id=order.agent_trade_id,
                symbol=order.symbol,
                filled=False,
                fill_price=0.0,
                reason="ltp_unavailable",
            )

        ot = order.order_type.upper()
        is_buy = order.direction.upper() == "BUY"

        if ot == "MARKET":
            slip = random.uniform(0, self._max_slippage)  # noqa: S311  # noqa: S311
            fp = current_ltp * (1 + (slip if is_buy else -slip))
            return PaperFill(
                agent_trade_id=order.agent_trade_id,
                symbol=order.symbol,
                filled=True,
                fill_price=round(fp, 2),
                simulated_slippage_pct=slip,
            )

        if (
            ot == "LIMIT"
            and order.price
            and (
                (is_buy and current_ltp <= order.price)
                or (not is_buy and current_ltp >= order.price)
            )
        ):
            return PaperFill(
                agent_trade_id=order.agent_trade_id,
                symbol=order.symbol,
                filled=True,
                fill_price=order.price,
            )

        if (
            ot in ("SL", "SL-M")
            and order.trigger_price
            and (
                (not is_buy and current_ltp <= order.trigger_price)
                or (is_buy and current_ltp >= order.trigger_price)
            )
        ):
            return PaperFill(
                agent_trade_id=order.agent_trade_id,
                symbol=order.symbol,
                filled=True,
                fill_price=current_ltp,
            )

        return PaperFill(
            agent_trade_id=order.agent_trade_id,
            symbol=order.symbol,
            filled=False,
            fill_price=0.0,
            reason=f"no_fill_condition:{ot}",
        )
