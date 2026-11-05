"""
Angel One (SmartAPI) response normalizer.

Pure functions. No I/O beyond loading error_map.json at import.
Translates raw SmartAPI response dicts to canonical broker models.

Field names below are taken from SmartAPI's published response examples
(smartapi.angelbroking.com/docs/Orders, /docs/Portfolio, /docs/User, /docs/Gtt)
as of 2026-09-22. Two gaps in that documentation are called out explicitly
below rather than guessed at: the full order-status enum, and whether
getPosition returns a live LTP/pnl field.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.brokers.base import BidAsk, BrokerError, MarketDepth, OptionQuote, Order, Position, Tick

_IST = ZoneInfo("Asia/Kolkata")

with (Path(__file__).parent / "error_map.json").open() as _f:
    _ERROR_MAP: dict[str, Any] = json.load(_f)

# SmartAPI order status/orderstatus -> canonical. Verified against the single
# documented getOrderBook example (status="cancelled") plus SmartAPI's commonly
# referenced status vocabulary; the full enum is NOT published in one place —
# confirm against real order-book responses before relying on an unlisted value
# (an unmapped status falls through to OPEN, the conservative choice: it keeps
# the order visible/tracked rather than silently dropping it as terminal).
_STATUS_MAP: dict[str, str] = {
    "COMPLETE": "FILLED",
    "OPEN": "OPEN",
    "OPEN PENDING": "OPEN",
    "TRIGGER PENDING": "OPEN",
    "PENDING": "OPEN",
    "MODIFIED": "OPEN",
    "MODIFY PENDING": "OPEN",
    "VALIDATION PENDING": "OPEN",
    "PUT ORDER REQ RECEIVED": "OPEN",
    "AFTER MARKET ORDER REQ RECEIVED": "OPEN",
    "CANCEL PENDING": "OPEN",
    "REJECTED": "REJECTED",
    "CANCELLED": "CANCELLED",
}

# SmartAPI producttype -> canonical. Angel One's own product strings already
# match the canonical vocabulary for the two products this system uses
# (see capability_matrix.json); CARRYFORWARD (F&O carry-forward) has no
# canonical equivalent here and maps to MARGIN as the closest match.
_PRODUCT_MAP: dict[str, str] = {
    "INTRADAY": "INTRADAY",
    "DELIVERY": "DELIVERY",
    "MARGIN": "MARGIN",
    "CARRYFORWARD": "MARGIN",
}


def _to_float(value: Any) -> float:
    """Parse a SmartAPI numeric-as-string field.

    Position rows have been observed with a space between a leading '-' and
    the digits (e.g. "- 2235.80" in netvalue) — plain float() raises on that.
    """
    if value is None:
        return 0.0
    text = str(value).replace(" ", "")
    return float(text) if text not in ("", "-") else 0.0


def _parse_ts(ts: str | datetime | None) -> datetime:
    """Parse a SmartAPI timestamp ("20-Oct-2020 13:10:59") to IST-aware datetime."""
    if ts is None or ts == "":
        return datetime.now(_IST)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=_IST)
    return datetime.strptime(str(ts), "%d-%b-%Y %H:%M:%S").replace(tzinfo=_IST)


def normalize_order(raw: dict[str, Any]) -> Order:
    """Translate a raw SmartAPI getOrderBook row to a canonical Order."""
    angel_status = str(raw.get("status") or raw.get("orderstatus") or "OPEN").upper()
    filled_qty = int(raw.get("filledshares") or 0)
    total_qty = int(raw.get("quantity") or 0)

    canonical_status = _STATUS_MAP.get(angel_status, "OPEN")
    if canonical_status == "OPEN" and 0 < filled_qty < total_qty:
        canonical_status = "PARTIAL"

    angel_product = str(raw.get("producttype", "INTRADAY")).upper()

    return Order(
        broker_order_id=str(raw["orderid"]),
        symbol=str(raw["tradingsymbol"]),
        direction=str(raw.get("transactiontype", "BUY")).upper(),
        quantity=total_qty,
        filled_quantity=filled_qty,
        pending_quantity=int(raw.get("unfilledshares") or 0),
        status=canonical_status,
        order_type=str(raw.get("ordertype", "MARKET")).upper(),
        product=_PRODUCT_MAP.get(angel_product, angel_product),
        average_price=_to_float(raw.get("averageprice")),
        tag=str(raw.get("ordertag") or ""),
        placed_at=_parse_ts(raw.get("updatetime") or raw.get("exchtime")),
        exchange_order_id=str(raw.get("exchangeorderid") or ""),
        rejection_reason=str(raw["text"]) if raw.get("text") else None,
        raw=raw,
    )


def normalize_position(raw: dict[str, Any], ltp: float = 0.0) -> Position:
    """Translate a raw SmartAPI getPosition row to a canonical Position.

    SmartAPI's documented getPosition response (docs/Portfolio) has no live
    ltp/pnl field on the row itself (unlike Kite, which returns both inline)
    — only static buy/sell/net quantities and prices. `ltp` must be supplied
    by the caller (get_positions() fetches it via get_ltp() before calling
    this); pnl is then derived here. Called with the default ltp=0.0 (e.g. in
    isolated unit tests) this intentionally returns last_price=pnl=0.0 rather
    than guessing a field name SmartAPI's docs don't show.
    """
    net_qty = int(_to_float(raw.get("netqty")))
    avg_price = _to_float(raw.get("avgnetprice"))
    angel_product = str(raw.get("producttype", "INTRADAY")).upper()
    sign = 1 if net_qty >= 0 else -1

    return Position(
        symbol=str(raw["tradingsymbol"]),
        exchange=str(raw.get("exchange", "NSE")),
        direction="long" if net_qty >= 0 else "short",
        quantity=abs(net_qty),
        average_price=avg_price,
        last_price=ltp,
        pnl=(ltp - avg_price) * abs(net_qty) * sign if ltp else 0.0,
        product=_PRODUCT_MAP.get(angel_product, angel_product),
    )


def normalize_error(raw: dict[str, Any]) -> BrokerError:
    """Map a SmartAPI exception dict to a canonical BrokerError.

    raw must contain: exception_type (str, the SmartApi.smartExceptions class
    name — TokenException/OrderException/InputException/...), message (str),
    code (str|int, the HTTP-style status SmartAPI attaches to the exception).
    """
    exc_type = str(raw.get("exception_type", "GeneralException"))
    message = str(raw.get("message", ""))
    original_code = str(raw.get("code", ""))

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
    """Normalize a SmartAPI getMarketData FULL-mode row (docs/MarketData) to a
    canonical OptionQuote. Field names (ltp, opnInterest, tradeVolume,
    depth.buy/sell[].{price,quantity,orders}) verified 2026-09-22 against
    SmartAPI's published FULL-mode example."""
    depth = raw.get("depth", {})
    buy_side: list[dict[str, Any]] = depth.get("buy", [])
    sell_side: list[dict[str, Any]] = depth.get("sell", [])
    best_bid = _to_float(buy_side[0]["price"]) if buy_side else 0.0
    best_ask = _to_float(sell_side[0]["price"]) if sell_side else 0.0

    return OptionQuote(
        symbol=symbol,
        ltp=_to_float(raw.get("ltp")),
        bid=best_bid,
        ask=best_ask,
        oi=int(_to_float(raw.get("opnInterest"))),
        volume=int(_to_float(raw.get("tradeVolume"))),
        prev_oi=0,
    )


def normalize_depth(symbol: str, raw: dict[str, Any]) -> MarketDepth:
    """Normalize a SmartAPI getMarketData FULL-mode row (docs/MarketData) to
    canonical MarketDepth. Verified 2026-09-22 against SmartAPI's published
    FULL-mode example — same depth.buy/sell shape as Kite's equivalent."""
    depth = raw.get("depth", {})
    ts = _parse_ts(raw.get("exchFeedTime"))

    def _side(entries: list[dict[str, Any]]) -> list[BidAsk]:
        return [
            BidAsk(
                price=_to_float(e.get("price")),
                quantity=int(_to_float(e.get("quantity"))),
                orders=int(_to_float(e.get("orders") or e.get("numberoforders"))),
            )
            for e in entries
        ]

    return MarketDepth(
        symbol=symbol,
        bids=_side(depth.get("buy", [])),
        asks=_side(depth.get("sell", [])),
        ltp=_to_float(raw.get("ltp")),
        timestamp=ts,
    )


def normalize_tick(raw: dict[str, Any]) -> Tick:
    """Normalize a SmartAPI getLtpData-shaped row to a canonical Tick.

    This is a REST-polling shape (exchange/tradingsymbol/symboltoken/ltp), not
    a parsed SmartWebSocketV2 binary tick — subscribe_ticks()/unsubscribe_ticks()
    on this adapter are bookkeeping stubs only (see adapter.py), matching
    KiteAdapter's pattern where the real websocket tick stream is owned by a
    separate ingester component. Wiring an actual SmartWebSocketV2 consumer
    that calls this normalizer is a separate, not-yet-built task.
    """
    return Tick(
        symbol=str(raw.get("tradingsymbol", "")),
        instrument_token=str(raw.get("symboltoken", "")),
        ltp=_to_float(raw.get("ltp")),
        bid=0.0,
        ask=0.0,
        volume=int(_to_float(raw.get("tradeVolume") or raw.get("tradevolume"))),
        exchange_ts=datetime.now(_IST),
        received_ts=datetime.now(_IST),
    )
