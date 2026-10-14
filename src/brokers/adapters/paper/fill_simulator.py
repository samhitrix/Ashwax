"""
Paper trading fill simulator.

Pure functions — no state, no I/O.
Given an OrderRequest and current LTP, determines if/how the order fills.

Fill rules:
  MARKET:  fill immediately at ltp +/- slippage
  LIMIT:   fill when ltp crosses the limit price
  SL:      fill at limit price when ltp crosses trigger_price
  SL-M:    fill at market (ltp) when ltp crosses trigger_price
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.brokers.base import Fill, OrderRequest

_IST = ZoneInfo("Asia/Kolkata")


def check_fill(
    order: OrderRequest,
    ltp: float,
    order_id: str,
    slippage_bps: float = 5.0,
) -> tuple[bool, Fill | None]:
    """
    Check if an order fills at the given ltp.

    Returns (filled, Fill) where Fill is None if the order does not fill.
    slippage_bps: basis points of slippage applied to MARKET and SL-M fills.
    """
    order_type = order.order_type.upper()
    direction = order.direction.upper()
    limit_price = order.price or 0.0
    trigger_price = order.trigger_price or 0.0
    slippage_factor = slippage_bps / 10_000.0

    fill_price: float | None = None

    if order_type == "MARKET":
        fill_price = (
            ltp * (1 + slippage_factor) if direction == "BUY" else ltp * (1 - slippage_factor)
        )

    elif order_type == "LIMIT":
        if direction == "BUY" and ltp <= limit_price or direction == "SELL" and ltp >= limit_price:
            fill_price = limit_price

    elif order_type == "SL":
        if (
            direction == "BUY"
            and ltp >= trigger_price
            or direction == "SELL"
            and ltp <= trigger_price
        ):
            fill_price = limit_price if limit_price > 0 else ltp

    elif order_type == "SL-M":
        if direction == "BUY" and ltp >= trigger_price:
            fill_price = ltp * (1 + slippage_factor)
        elif direction == "SELL" and ltp <= trigger_price:
            fill_price = ltp * (1 - slippage_factor)

    if fill_price is None:
        return False, None

    now = datetime.now(_IST)
    fill = Fill(
        order_id=order_id,
        trade_id=f"PAPER-TRADE-{order_id}",
        quantity=order.quantity,
        price=round(fill_price, 2),
        exchange_time=now,
    )
    return True, fill


def check_gtt_triggers(
    sl_trigger: float,
    target_trigger: float | None,
    ltp: float,
) -> str | None:
    """
    Check if a GTT order triggers at the given ltp.

    Returns "SL" if SL leg triggers, "TARGET" if target leg triggers, None if neither.
    Assumes the position is LONG (SELL legs).
    """
    if ltp <= sl_trigger:
        return "SL"
    if target_trigger is not None and ltp >= target_trigger:
        return "TARGET"
    return None
