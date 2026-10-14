"""Unit tests for src/brokers/base.py — canonical models and BrokerInterface ABC."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pytest
from src.brokers.base import (
    BidAsk,
    BrokerError,
    BrokerInterface,
    Fill,
    GTTOrder,
    GTTRequest,
    Holding,
    Instrument,
    Margin,
    MarketDepth,
    OptionQuote,
    Order,
    OrderMarginResult,
    OrderRequest,
    Position,
    SessionInfo,
    SessionStatus,
    Tick,
)

NOW = datetime(2026, 1, 15, 9, 30, 0)


# ── Canonical model instantiation ─────────────────────────────────────────────


def test_fill_instantiation() -> None:
    f = Fill(order_id="O1", trade_id="T1", quantity=10, price=500.0, exchange_time=NOW)
    assert f.order_id == "O1"
    assert f.quantity == 10


def test_order_request_instantiation() -> None:
    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        direction="BUY",
        quantity=5,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id="AT1",
        signal_id="SIG1",
    )
    assert req.price is None
    assert req.trigger_price is None
    assert req.tag == ""


def test_order_request_with_limit_price() -> None:
    req = OrderRequest(
        symbol="TCS",
        exchange="NSE",
        direction="SELL",
        quantity=1,
        order_type="LIMIT",
        product="DELIVERY",
        validity="DAY",
        agent_trade_id="AT2",
        signal_id="SIG2",
        price=3500.0,
    )
    assert req.price == 3500.0


def test_order_instantiation() -> None:
    order = Order(
        broker_order_id="BRK1",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        filled_quantity=10,
        pending_quantity=0,
        status="FILLED",
        order_type="MARKET",
        product="INTRADAY",
        average_price=1500.0,
        tag="tag1",
        placed_at=NOW,
    )
    assert order.fills == []
    assert order.exchange_order_id == ""
    assert order.rejection_reason is None
    assert order.raw == {}


def test_order_with_fills() -> None:
    fill = Fill(order_id="BRK1", trade_id="T1", quantity=10, price=1500.0, exchange_time=NOW)
    order = Order(
        broker_order_id="BRK1",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        filled_quantity=10,
        pending_quantity=0,
        status="FILLED",
        order_type="MARKET",
        product="INTRADAY",
        average_price=1500.0,
        tag="",
        placed_at=NOW,
        fills=[fill],
    )
    assert len(order.fills) == 1
    assert order.fills[0].trade_id == "T1"


def test_tick_instantiation() -> None:
    tick = Tick(
        symbol="NIFTY50",
        instrument_token="256265",
        ltp=22000.0,
        bid=21999.5,
        ask=22000.5,
        volume=1000000,
        exchange_ts=NOW,
        received_ts=NOW,
    )
    assert tick.ltp == 22000.0


def test_gtt_request_instantiation() -> None:
    gtt = GTTRequest(
        symbol="HDFC",
        product="DELIVERY",
        gtt_type="TWO_LEG",
        quantity=5,
        agent_trade_id="AT3",
        sl_trigger=1400.0,
        sl_order_type="MARKET",
        target_trigger=1600.0,
    )
    assert gtt.sl_price is None
    assert gtt.target_price is None


def test_gtt_order_instantiation() -> None:
    gtt_order = GTTOrder(
        gtt_id="GTT1",
        symbol="HDFC",
        status="ACTIVE",
        gtt_type="TWO_LEG",
        sl_trigger=1400.0,
        target_trigger=1600.0,
    )
    assert gtt_order.raw == {}


def test_position_instantiation() -> None:
    pos = Position(
        symbol="RELIANCE",
        exchange="NSE",
        direction="long",
        quantity=10,
        average_price=2500.0,
        last_price=2550.0,
        pnl=500.0,
        product="INTRADAY",
    )
    assert pos.pnl == 500.0


def test_holding_instantiation() -> None:
    h = Holding(
        symbol="TCS",
        exchange="NSE",
        quantity=5,
        average_price=3000.0,
        last_price=3200.0,
        pnl=1000.0,
    )
    assert h.pnl == 1000.0


def test_margin_instantiation() -> None:
    m = Margin(equity=200000.0, available_cash=150000.0, used_margin=50000.0)
    assert m.collateral == 0.0


def test_margin_with_collateral() -> None:
    m = Margin(equity=300000.0, available_cash=200000.0, used_margin=50000.0, collateral=50000.0)
    assert m.collateral == 50000.0


def test_bid_ask_instantiation() -> None:
    ba = BidAsk(price=100.0, quantity=500, orders=3)
    assert ba.orders == 3


def test_market_depth_instantiation() -> None:
    bid = BidAsk(price=99.5, quantity=100, orders=1)
    ask = BidAsk(price=100.5, quantity=200, orders=2)
    depth = MarketDepth(symbol="SBI", bids=[bid], asks=[ask], ltp=100.0, timestamp=NOW)
    assert len(depth.bids) == 1
    assert len(depth.asks) == 1


def test_instrument_instantiation() -> None:
    inst = Instrument(
        symbol="NIFTY2612117500CE",
        exchange="NFO",
        instrument_token="12345",
        instrument_type="CE",
        lot_size=50,
        tick_size=0.05,
        strike=17500.0,
    )
    assert inst.expiry is None


def test_session_info_instantiation() -> None:
    info = SessionInfo(
        access_token="token123",
        user_id="ABC123",
        login_time=NOW,
    )
    assert info.expires_at is None


def test_session_status_instantiation() -> None:
    status = SessionStatus(is_valid=True, user_id="ABC123")
    assert status.message == ""


def test_broker_error_instantiation() -> None:
    err = BrokerError(code="MARGIN", message="Insufficient margin", retryable=False)
    assert err.original_code == ""
    assert err.original_message == ""


def test_broker_error_retryable() -> None:
    err = BrokerError(
        code="TOKEN",
        message="Access token expired",
        retryable=True,
        original_code="session_expired",
        original_message="Session has expired",
    )
    assert err.retryable is True
    assert err.original_code == "session_expired"


# ── BrokerInterface ABC ────────────────────────────────────────────────────────


def test_broker_interface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BrokerInterface()  # type: ignore[abstract]


def test_concrete_broker_implements_all_methods() -> None:
    """A fully concrete subclass must be instantiable."""

    class _FakeBroker(BrokerInterface):
        async def login(self) -> SessionInfo:
            return SessionInfo(access_token="t", user_id="u", login_time=NOW)

        async def refresh_token(self) -> SessionInfo:
            return SessionInfo(access_token="t", user_id="u", login_time=NOW)

        async def get_session_status(self) -> SessionStatus:
            return SessionStatus(is_valid=True, user_id="u")

        async def subscribe_ticks(self, tokens: list[str]) -> None:
            pass

        async def unsubscribe_ticks(self, tokens: list[str]) -> None:
            pass

        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            return {}

        async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
            return pd.DataFrame()

        async def get_depth(self, symbol: str) -> MarketDepth:
            return MarketDepth(symbol=symbol, bids=[], asks=[], ltp=0.0, timestamp=NOW)

        async def get_instruments(self, exchange: str) -> list[Instrument]:
            return []

        async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
            return {}

        async def place_order(self, order: OrderRequest) -> str:
            return "ORDER1"

        async def modify_order(self, order_id: str, params: dict[str, Any]) -> str:
            return order_id

        async def cancel_order(self, order_id: str) -> bool:
            return True

        async def get_order_status(self, order_id: str) -> Order:
            return Order(
                broker_order_id=order_id,
                symbol="X",
                direction="BUY",
                quantity=1,
                filled_quantity=0,
                pending_quantity=1,
                status="OPEN",
                order_type="MARKET",
                product="INTRADAY",
                average_price=0.0,
                tag="",
                placed_at=NOW,
            )

        async def get_all_orders(self) -> list[Order]:
            return []

        async def get_trades(self, order_id: str) -> list[Fill]:
            return []

        async def get_order_margin(self, orders: list[OrderRequest]) -> OrderMarginResult:
            return OrderMarginResult(total_margin=0.0)

        async def get_positions(self) -> list[Position]:
            return []

        async def get_holdings(self) -> list[Holding]:
            return []

        async def get_margins(self) -> Margin:
            return Margin(equity=0.0, available_cash=0.0, used_margin=0.0)

        async def place_gtt(self, gtt: GTTRequest) -> str:
            return "GTT1"

        async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str:
            return gtt_id

        async def cancel_gtt(self, gtt_id: str) -> bool:
            return True

        async def get_all_gtts(self) -> list[GTTOrder]:
            return []

        def supports_gtt(self) -> bool:
            return True

        def supports_bracket_order(self) -> bool:
            return False

        def supports_cover_order(self) -> bool:
            return False

        def normalize_tick(self, raw: dict[str, Any]) -> Tick:
            return Tick(
                symbol="X",
                instrument_token="1",
                ltp=0.0,
                bid=0.0,
                ask=0.0,
                volume=0,
                exchange_ts=NOW,
                received_ts=NOW,
            )

        def normalize_order(self, raw: dict[str, Any]) -> Order:
            return Order(
                broker_order_id="X",
                symbol="X",
                direction="BUY",
                quantity=1,
                filled_quantity=0,
                pending_quantity=1,
                status="OPEN",
                order_type="MARKET",
                product="INTRADAY",
                average_price=0.0,
                tag="",
                placed_at=NOW,
            )

        def normalize_position(self, raw: dict[str, Any]) -> Position:
            return Position(
                symbol="X",
                exchange="NSE",
                direction="long",
                quantity=1,
                average_price=0.0,
                last_price=0.0,
                pnl=0.0,
                product="INTRADAY",
            )

        def normalize_error(self, raw: dict[str, Any]) -> BrokerError:
            return BrokerError(code="UNKNOWN", message="", retryable=False)

    broker = _FakeBroker()
    assert broker.supports_gtt() is True
    assert broker.supports_bracket_order() is False
