"""Unit tests for src/brokers/adapters/kite/normalizer.py."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.brokers.adapters.kite.normalizer import (
    normalize_depth,
    normalize_error,
    normalize_option_quote,
    normalize_order,
    normalize_position,
    normalize_tick,
)

_IST = ZoneInfo("Asia/Kolkata")
_TS = "2026-01-15 09:30:00"


# ── normalize_order ────────────────────────────────────────────────────────────


def _order_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    base = {
        "order_id": "ORD001",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "filled_quantity": 0,
        "pending_quantity": 10,
        "status": "OPEN",
        "order_type": "MARKET",
        "product": "MIS",
        "average_price": 0.0,
        "tag": "",
        "order_timestamp": _TS,
        "exchange_order_id": "EXC001",
        "status_message": None,
    }
    base.update(overrides)
    return base


def test_normalize_order_open_market() -> None:
    order = normalize_order(_order_raw())
    assert order.broker_order_id == "ORD001"
    assert order.symbol == "INFY"
    assert order.direction == "BUY"
    assert order.status == "OPEN"
    assert order.product == "INTRADAY"
    assert order.placed_at == datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


def test_normalize_order_complete_becomes_filled() -> None:
    order = normalize_order(_order_raw(status="COMPLETE", filled_quantity=10, pending_quantity=0))
    assert order.status == "FILLED"
    assert order.filled_quantity == 10


def test_normalize_order_partial_fill() -> None:
    order = normalize_order(_order_raw(filled_quantity=5, pending_quantity=5))
    assert order.status == "PARTIAL"


def test_normalize_order_rejected() -> None:
    order = normalize_order(_order_raw(status="REJECTED", status_message="Insufficient margin"))
    assert order.status == "REJECTED"
    assert order.rejection_reason == "Insufficient margin"


def test_normalize_order_cancelled() -> None:
    order = normalize_order(_order_raw(status="CANCELLED"))
    assert order.status == "CANCELLED"


def test_normalize_order_cancelled_amo() -> None:
    order = normalize_order(_order_raw(status="CANCELLED AMO"))
    assert order.status == "CANCELLED"


def test_normalize_order_trigger_pending_is_open() -> None:
    order = normalize_order(_order_raw(status="TRIGGER PENDING"))
    assert order.status == "OPEN"


def test_normalize_order_cnc_product() -> None:
    order = normalize_order(_order_raw(product="CNC"))
    assert order.product == "DELIVERY"


def test_normalize_order_nrml_product() -> None:
    order = normalize_order(_order_raw(product="NRML"))
    assert order.product == "MARGIN"


def test_normalize_order_average_price_none_becomes_zero() -> None:
    order = normalize_order(_order_raw(average_price=None))
    assert order.average_price == 0.0


def test_normalize_order_tag_none_becomes_empty() -> None:
    order = normalize_order(_order_raw(tag=None))
    assert order.tag == ""


# ── normalize_tick ─────────────────────────────────────────────────────────────


def _tick_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    base = {
        "instrument_token": 408065,
        "tradingsymbol": "INFY",
        "last_price": 1500.0,
        "volume": 500000,
        "exchange_timestamp": _TS,
        "depth": {
            "buy": [{"price": 1499.5, "quantity": 100, "orders": 3}],
            "sell": [{"price": 1500.5, "quantity": 200, "orders": 5}],
        },
    }
    base.update(overrides)
    return base


def test_normalize_tick_basic() -> None:
    tick = normalize_tick(_tick_raw())
    assert tick.symbol == "INFY"
    assert tick.instrument_token == "408065"
    assert tick.ltp == 1500.0
    assert tick.bid == 1499.5
    assert tick.ask == 1500.5
    assert tick.volume == 500000
    assert tick.exchange_ts == datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


def test_normalize_tick_no_depth_gives_zero_bid_ask() -> None:
    tick = normalize_tick(_tick_raw(depth={}))
    assert tick.bid == 0.0
    assert tick.ask == 0.0


def test_normalize_tick_volume_traded_fallback() -> None:
    raw = _tick_raw()
    del raw["volume"]
    raw["volume_traded"] = 999
    tick = normalize_tick(raw)
    assert tick.volume == 999


def test_normalize_tick_last_trade_time_fallback() -> None:
    raw = _tick_raw()
    del raw["exchange_timestamp"]
    raw["last_trade_time"] = _TS
    tick = normalize_tick(raw)
    assert tick.exchange_ts == datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


# ── normalize_position ─────────────────────────────────────────────────────────


def _position_raw(**overrides: object) -> dict:  # type: ignore[type-arg]
    base = {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "quantity": 10,
        "average_price": 2500.0,
        "last_price": 2550.0,
        "pnl": 500.0,
        "product": "MIS",
    }
    base.update(overrides)
    return base


def test_normalize_position_long() -> None:
    pos = normalize_position(_position_raw())
    assert pos.direction == "long"
    assert pos.quantity == 10
    assert pos.product == "INTRADAY"


def test_normalize_position_short() -> None:
    pos = normalize_position(_position_raw(quantity=-5))
    assert pos.direction == "short"
    assert pos.quantity == 5


def test_normalize_position_zero_is_long() -> None:
    pos = normalize_position(_position_raw(quantity=0))
    assert pos.direction == "long"
    assert pos.quantity == 0


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


def test_normalize_error_circuit_message_override() -> None:
    err = normalize_error(
        {
            "exception_type": "OrderException",
            "message": "Stock has hit circuit limit",
            "code": "400",
        }
    )
    assert err.code == "CIRCUIT"
    assert err.retryable is False


def test_normalize_error_rate_limit_override() -> None:
    err = normalize_error(
        {"exception_type": "GeneralException", "message": "Too many requests sent", "code": "429"}
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
        {"exception_type": "PermissionException", "message": "Not allowed", "code": "403"}
    )
    assert err.original_code == "403"
    assert err.original_message == "Not allowed"


# ── normalize_depth ────────────────────────────────────────────────────────────


def test_normalize_depth() -> None:
    raw = {
        "last_price": 1500.0,
        "exchange_timestamp": _TS,
        "depth": {
            "buy": [
                {"price": 1499.5, "quantity": 100, "orders": 3},
                {"price": 1499.0, "quantity": 200, "orders": 5},
            ],
            "sell": [
                {"price": 1500.5, "quantity": 150, "orders": 2},
                {"price": 1501.0, "quantity": 300, "orders": 4},
            ],
        },
    }
    depth = normalize_depth("INFY", raw)
    assert depth.symbol == "INFY"
    assert depth.ltp == 1500.0
    assert len(depth.bids) == 2
    assert len(depth.asks) == 2
    assert depth.bids[0].price == 1499.5
    assert depth.asks[0].quantity == 150


def test_normalize_depth_empty_sides() -> None:
    raw = {"last_price": 100.0, "depth": {}}
    depth = normalize_depth("SBI", raw)
    assert depth.bids == []
    assert depth.asks == []


# ── normalize_option_quote ─────────────────────────────────────────────────────


def test_normalize_option_quote_full() -> None:
    raw = {
        "last_price": 120.5,
        "oi": 45000,
        "volume": 8200,
        "oi_day_low": 41000,
        "depth": {
            "buy": [{"price": 120.0, "quantity": 50, "orders": 3}],
            "sell": [{"price": 121.0, "quantity": 75, "orders": 4}],
        },
    }
    q = normalize_option_quote("NFO:RELIANCE24JUL2800CE", raw)
    assert q.symbol == "NFO:RELIANCE24JUL2800CE"
    assert q.ltp == 120.5
    assert q.bid == 120.0
    assert q.ask == 121.0
    assert q.oi == 45000
    assert q.volume == 8200
    assert q.prev_oi == 41000
    assert q.mid == 120.5  # (120.0 + 121.0) / 2


def test_normalize_option_quote_empty_book_falls_back_to_ltp() -> None:
    raw = {"last_price": 55.0, "oi": 1000, "volume": 200, "depth": {}}
    q = normalize_option_quote("NFO:TCS24JUL3900CE", raw)
    assert q.bid == 0.0
    assert q.ask == 0.0
    assert q.mid == 55.0  # falls back to ltp when book empty


def test_normalize_option_quote_missing_oi_volume_default_zero() -> None:
    raw = {"last_price": 10.0, "depth": {}}
    q = normalize_option_quote("NFO:X", raw)
    assert q.oi == 0
    assert q.volume == 0
    assert q.prev_oi == 0
