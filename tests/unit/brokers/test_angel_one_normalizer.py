"""Unit tests for src/brokers/adapters/angel_one/normalizer.py."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.brokers.adapters.angel_one.normalizer import (
    normalize_depth,
    normalize_error,
    normalize_option_quote,
    normalize_order,
    normalize_position,
    normalize_tick,
)

_IST = ZoneInfo("Asia/Kolkata")
_TS = "20-Oct-2020 13:10:59"


# ── normalize_order ────────────────────────────────────────────────────────────


def _order_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    # Shape verified 2026-09-22 against smartapi.angelbroking.com/docs/Orders
    # getOrderBook published example.
    base = {
        "orderid": "201020000000080",
        "tradingsymbol": "SBIN-EQ",
        "transactiontype": "BUY",
        "quantity": "10",
        "filledshares": "0",
        "unfilledshares": "10",
        "status": "open",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "averageprice": "0",
        "ordertag": "",
        "updatetime": _TS,
        "exchangeorderid": "1100000000048358",
        "text": "",
    }
    base.update(overrides)
    return base


def test_normalize_order_open_market() -> None:
    order = normalize_order(_order_raw())
    assert order.broker_order_id == "201020000000080"
    assert order.symbol == "SBIN-EQ"
    assert order.direction == "BUY"
    assert order.status == "OPEN"
    assert order.product == "INTRADAY"
    assert order.placed_at == datetime(2020, 10, 20, 13, 10, 59, tzinfo=_IST)


def test_normalize_order_complete_becomes_filled() -> None:
    order = normalize_order(_order_raw(status="complete", filledshares="10", unfilledshares="0"))
    assert order.status == "FILLED"
    assert order.filled_quantity == 10


def test_normalize_order_partial_fill() -> None:
    order = normalize_order(_order_raw(filledshares="5", unfilledshares="5"))
    assert order.status == "PARTIAL"


def test_normalize_order_rejected() -> None:
    order = normalize_order(_order_raw(status="rejected", text="Insufficient margin"))
    assert order.status == "REJECTED"
    assert order.rejection_reason == "Insufficient margin"


def test_normalize_order_cancelled() -> None:
    order = normalize_order(_order_raw(status="cancelled"))
    assert order.status == "CANCELLED"


def test_normalize_order_trigger_pending_is_open() -> None:
    order = normalize_order(_order_raw(status="trigger pending"))
    assert order.status == "OPEN"


def test_normalize_order_delivery_product() -> None:
    order = normalize_order(_order_raw(producttype="DELIVERY"))
    assert order.product == "DELIVERY"


def test_normalize_order_carryforward_maps_to_margin() -> None:
    order = normalize_order(_order_raw(producttype="CARRYFORWARD"))
    assert order.product == "MARGIN"


def test_normalize_order_average_price_zero_string() -> None:
    order = normalize_order(_order_raw(averageprice="0"))
    assert order.average_price == 0.0


def test_normalize_order_no_text_gives_no_rejection_reason() -> None:
    order = normalize_order(_order_raw(text=""))
    assert order.rejection_reason is None


def test_normalize_order_falls_back_to_orderstatus_field() -> None:
    raw = _order_raw()
    del raw["status"]
    raw["orderstatus"] = "cancelled"
    order = normalize_order(raw)
    assert order.status == "CANCELLED"


# ── normalize_position ─────────────────────────────────────────────────────────


def _position_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    # Shape verified 2026-09-22 against docs/Portfolio getPosition example —
    # note there is NO ltp/pnl field on the raw row (unlike Kite); those are
    # supplied by the caller (get_positions() enriches via get_ltp()).
    base = {
        "exchange": "NSE",
        "symboltoken": "2885",
        "producttype": "DELIVERY",
        "tradingsymbol": "RELIANCE-EQ",
        "netqty": "1",
        "avgnetprice": "2235.80",
        "netvalue": "- 2235.80",
    }
    base.update(overrides)
    return base


def test_normalize_position_long() -> None:
    pos = normalize_position(_position_raw())
    assert pos.direction == "long"
    assert pos.quantity == 1
    assert pos.product == "DELIVERY"
    assert pos.average_price == 2235.80


def test_normalize_position_short() -> None:
    pos = normalize_position(_position_raw(netqty="-5"))
    assert pos.direction == "short"
    assert pos.quantity == 5


def test_normalize_position_zero_is_long() -> None:
    pos = normalize_position(_position_raw(netqty="0"))
    assert pos.direction == "long"
    assert pos.quantity == 0


def test_normalize_position_default_ltp_gives_zero_pnl() -> None:
    pos = normalize_position(_position_raw())
    assert pos.last_price == 0.0
    assert pos.pnl == 0.0


def test_normalize_position_enriched_ltp_computes_pnl() -> None:
    pos = normalize_position(_position_raw(netqty="2", avgnetprice="100.0"), ltp=110.0)
    assert pos.last_price == 110.0
    assert pos.pnl == 20.0  # (110 - 100) * 2


def test_normalize_position_short_pnl_sign() -> None:
    pos = normalize_position(_position_raw(netqty="-2", avgnetprice="100.0"), ltp=90.0)
    # short 2 @ 100, now 90 -> profit of 20
    assert pos.pnl == 20.0


# ── normalize_error ────────────────────────────────────────────────────────────


def test_normalize_error_token_exception() -> None:
    err = normalize_error(
        {"exception_type": "TokenException", "message": "Invalid token", "code": "403"}
    )
    assert err.code == "TOKEN"
    assert err.retryable is True


def test_normalize_error_network_exception() -> None:
    err = normalize_error(
        {"exception_type": "NetworkException", "message": "Connection refused", "code": "503"}
    )
    assert err.code == "NETWORK"
    assert err.retryable is True


def test_normalize_error_data_exception_not_retryable() -> None:
    err = normalize_error(
        {"exception_type": "DataException", "message": "Bad OMS response", "code": "502"}
    )
    assert err.code == "DATA"
    assert err.retryable is False


def test_normalize_error_margin_message_override() -> None:
    err = normalize_error(
        {
            "exception_type": "OrderException",
            "message": "Insufficient margin available",
            "code": "400",
        }
    )
    assert err.code == "MARGIN"
    assert err.retryable is False


def test_normalize_error_rate_limit_override() -> None:
    err = normalize_error(
        {"exception_type": "GeneralException", "message": "Too many requests sent", "code": "429"}
    )
    assert err.code == "RATE_LIMIT"
    assert err.retryable is True


def test_normalize_error_access_denied_is_rate_limit() -> None:
    err = normalize_error(
        {"exception_type": "PermissionException", "message": "Access denied", "code": "403"}
    )
    assert err.code == "RATE_LIMIT"
    assert err.retryable is True


def test_normalize_error_unknown_exception_type() -> None:
    err = normalize_error(
        {"exception_type": "WeirdException", "message": "Something broke", "code": "500"}
    )
    assert err.code == "UNKNOWN"
    assert err.retryable is False


def test_normalize_error_preserves_original_fields() -> None:
    err = normalize_error(
        {"exception_type": "InputException", "message": "Bad input", "code": "400"}
    )
    assert err.original_code == "400"
    assert err.original_message == "Bad input"


# ── normalize_depth / normalize_option_quote (getMarketData FULL mode) ────────


def _market_data_row(**overrides: object) -> dict:  # type: ignore[type-arg]
    # Shape verified 2026-09-22 against docs/MarketData's published FULL-mode
    # example (note camelCase field names, unlike the lowercase order/position
    # endpoints — this is a real inconsistency in SmartAPI, not a typo here).
    base = {
        "exchange": "NSE",
        "tradingSymbol": "SBIN-EQ",
        "symbolToken": "3045",
        "ltp": 568.2,
        "exchFeedTime": _TS,
        "tradeVolume": 3556150,
        "opnInterest": 0,
        "depth": {
            "buy": [
                {"price": 568.2, "quantity": 511, "orders": 2},
                {"price": 568.15, "quantity": 411, "orders": 2},
            ],
            "sell": [{"price": 568.25, "quantity": 3348, "orders": 5}],
        },
    }
    base.update(overrides)
    return base


def test_normalize_depth() -> None:
    depth = normalize_depth("SBIN-EQ", _market_data_row())
    assert depth.symbol == "SBIN-EQ"
    assert depth.ltp == 568.2
    assert len(depth.bids) == 2
    assert len(depth.asks) == 1
    assert depth.bids[0].price == 568.2
    assert depth.asks[0].quantity == 3348
    assert depth.timestamp == datetime(2020, 10, 20, 13, 10, 59, tzinfo=_IST)


def test_normalize_depth_empty_sides() -> None:
    depth = normalize_depth("SBIN-EQ", _market_data_row(depth={}))
    assert depth.bids == []
    assert depth.asks == []


def test_normalize_option_quote_full() -> None:
    q = normalize_option_quote(
        "NFO:RELIANCE24JUL2800CE",
        _market_data_row(ltp=120.5, opnInterest=45000, tradeVolume=8200),
    )
    assert q.symbol == "NFO:RELIANCE24JUL2800CE"
    assert q.ltp == 120.5
    assert q.oi == 45000
    assert q.volume == 8200
    assert q.prev_oi == 0


def test_normalize_option_quote_empty_book_falls_back_to_ltp() -> None:
    q = normalize_option_quote("NFO:TCS24JUL3900CE", _market_data_row(ltp=55.0, depth={}))
    assert q.bid == 0.0
    assert q.ask == 0.0
    assert q.mid == 55.0


# ── normalize_tick ─────────────────────────────────────────────────────────────


def test_normalize_tick_basic() -> None:
    # Shape verified 2026-09-22 against docs/Orders getLtpData example.
    raw = {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "ltp": "191"}
    tick = normalize_tick(raw)
    assert tick.symbol == "SBIN-EQ"
    assert tick.instrument_token == "3045"
    assert tick.ltp == 191.0
    assert tick.bid == 0.0
    assert tick.ask == 0.0


def test_normalize_tick_missing_symbol_defaults_empty() -> None:
    tick = normalize_tick({"ltp": "100"})
    assert tick.symbol == ""
    assert tick.instrument_token == ""
