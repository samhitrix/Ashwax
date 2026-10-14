"""
Kite broker response normalizer.

Pure functions. No I/O beyond loading error_map.json at import.
Translates raw kiteconnect API dicts to canonical broker models.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.brokers.base import (
    BidAsk,
    BrokerError,
    MarketDepth,
    OptionQuote,
    Order,
    Position,
    Tick,
)

_IST = ZoneInfo("Asia/Kolkata")

with (Path(__file__).parent / "error_map.json").open() as _f:
    _ERROR_MAP: dict[str, Any] = json.load(_f)

# Kite order status → canonical
_STATUS_MAP: dict[str, str] = {
    "COMPLETE": "FILLED",
    "OPEN": "OPEN",
    "OPEN PENDING": "OPEN",
    "TRIGGER PENDING": "OPEN",
    "AMO REQ RECEIVED": "OPEN",
    "AFTER MARKET ORDER REQ RECEIVED": "OPEN",
    "PUT ORDER REQ RECEIVED": "OPEN",
    "VALIDATION PENDING": "OPEN",
    "MODIFY PENDING": "OPEN",
    "CANCEL PENDING": "OPEN",
    "MODIFY VALIDATION PENDING": "OPEN",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
    "CANCELLED AMO": "CANCELLED",
}

# Kite product code → canonical
_PRODUCT_MAP: dict[str, str] = {
    "MIS": "INTRADAY",
    "CNC": "DELIVERY",
    "NRML": "MARGIN",
}


def _parse_ts(ts: str | datetime | None) -> datetime:
    """Parse Kite timestamp string or datetime to IST-aware datetime."""
    if ts is None:
        return datetime.now(_IST)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=_IST)
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)


def normalize_order(raw: dict[str, Any]) -> Order:
    """Translate a raw Kite order dict to a canonical Order."""
    kite_status = str(raw.get("status", "OPEN")).upper()
    filled_qty = int(raw.get("filled_quantity", 0))
    total_qty = int(raw.get("quantity", 0))

    canonical_status = _STATUS_MAP.get(kite_status, "OPEN")
    if canonical_status == "OPEN" and 0 < filled_qty < total_qty:
        canonical_status = "PARTIAL"

    kite_product = str(raw.get("product", "MIS")).upper()

    return Order(
        broker_order_id=str(raw["order_id"]),
        symbol=str(raw["tradingsymbol"]),
        direction=str(raw.get("transaction_type", "BUY")).upper(),
        quantity=total_qty,
        filled_quantity=filled_qty,
        pending_quantity=int(raw.get("pending_quantity", 0)),
        status=canonical_status,
        order_type=str(raw.get("order_type", "MARKET")).upper(),
        product=_PRODUCT_MAP.get(kite_product, kite_product),
        average_price=float(raw.get("average_price") or 0.0),
        tag=str(raw.get("tag") or ""),
        placed_at=_parse_ts(raw.get("order_timestamp")),
        exchange_order_id=str(raw.get("exchange_order_id") or ""),
        rejection_reason=raw.get("status_message") or None,
        raw=raw,
    )


def normalize_tick(raw: dict[str, Any]) -> Tick:
    """Normalize a Kite REST quote or WebSocket full-mode tick."""
    depth = raw.get("depth", {})
    buy_side: list[dict[str, Any]] = depth.get("buy", [])
    sell_side: list[dict[str, Any]] = depth.get("sell", [])
    best_bid = float(buy_side[0]["price"]) if buy_side else 0.0
    best_ask = float(sell_side[0]["price"]) if sell_side else 0.0

    ts = _parse_ts(raw.get("exchange_timestamp") or raw.get("last_trade_time"))

    return Tick(
        symbol=str(raw.get("tradingsymbol", "")),
        instrument_token=str(raw["instrument_token"]),
        ltp=float(raw.get("last_price", 0.0)),
        bid=best_bid,
        ask=best_ask,
        volume=int(raw.get("volume") or raw.get("volume_traded") or 0),
        exchange_ts=ts,
        received_ts=datetime.now(_IST),
    )


def normalize_position(raw: dict[str, Any]) -> Position:
    """Translate a raw Kite position dict to a canonical Position."""
    quantity = int(raw.get("quantity", 0))
    kite_product = str(raw.get("product", "MIS")).upper()

    return Position(
        symbol=str(raw["tradingsymbol"]),
        exchange=str(raw.get("exchange", "NSE")),
        direction="long" if quantity >= 0 else "short",
        quantity=abs(quantity),
        average_price=float(raw.get("average_price", 0.0)),
        last_price=float(raw.get("last_price", 0.0)),
        pnl=float(raw.get("pnl", 0.0)),
        product=_PRODUCT_MAP.get(kite_product, kite_product),
    )


def normalize_error(raw: dict[str, Any]) -> BrokerError:
    """Map a kiteconnect exception dict to a canonical BrokerError.

    raw must contain: exception_type (str), message (str), code (str|int).
    """
    exc_type = str(raw.get("exception_type", "GeneralException"))
    message = str(raw.get("message", ""))
    original_code = str(raw.get("code", ""))

    # Message overrides take priority (most specific match first)
    msg_lower = message.lower()
    for override in _ERROR_MAP.get("message_overrides", []):
        if override["contains"] in msg_lower:
            return BrokerError(
                code=override["code"],
                message=message,
                retryable=bool(override["retryable"]),
                original_code=original_code,
                original_message=message,
            )

    type_entry: dict[str, Any] = _ERROR_MAP.get("exception_type_map", {}).get(exc_type, {})
    return BrokerError(
        code=str(type_entry.get("code", "UNKNOWN")),
        message=message,
        retryable=bool(type_entry.get("retryable", False)),
        original_code=original_code,
        original_message=message,
    )


def normalize_option_quote(symbol: str, raw: dict[str, Any]) -> OptionQuote:
    """Normalize a Kite quote() entry to a canonical OptionQuote.

    Kite quote() returns ``oi``, ``volume`` (day volume) and a ``depth`` book for
    derivatives. Best bid/ask come from the top of the book; ltp from last_price.
    """
    depth = raw.get("depth", {})
    buy_side: list[dict[str, Any]] = depth.get("buy", [])
    sell_side: list[dict[str, Any]] = depth.get("sell", [])
    best_bid = float(buy_side[0]["price"]) if buy_side else 0.0
    best_ask = float(sell_side[0]["price"]) if sell_side else 0.0

    return OptionQuote(
        symbol=symbol,
        ltp=float(raw.get("last_price", 0.0)),
        bid=best_bid,
        ask=best_ask,
        oi=int(raw.get("oi") or 0),
        volume=int(raw.get("volume") or raw.get("volume_traded") or 0),
        prev_oi=int(raw.get("oi_day_low") or 0),
    )


def normalize_depth(symbol: str, raw: dict[str, Any]) -> MarketDepth:
    """Normalize a Kite full market depth response to canonical MarketDepth."""
    depth = raw.get("depth", {})
    ts = _parse_ts(raw.get("timestamp") or raw.get("exchange_timestamp"))

    def _side(entries: list[dict[str, Any]]) -> list[BidAsk]:
        return [
            BidAsk(
                price=float(e["price"]),
                quantity=int(e["quantity"]),
                orders=int(e["orders"]),
            )
            for e in entries
        ]

    return MarketDepth(
        symbol=symbol,
        bids=_side(depth.get("buy", [])),
        asks=_side(depth.get("sell", [])),
        ltp=float(raw.get("last_price", 0.0)),
        timestamp=ts,
    )
