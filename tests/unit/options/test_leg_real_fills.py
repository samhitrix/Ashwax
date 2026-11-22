"""Live option legs use the broker's real fill, on entry and on close, never a pre-order quote.
All values are made up."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface, Order
from src.options.options_leg_executor import LegFillState, OptionsLegExecutor
from src.options.structure_selector import OptionLeg


def _leg() -> OptionLeg:
    return OptionLeg(
        tradingsymbol="BFO:INDEX26JAN20000PE",
        instrument_type="PE",
        strike=78800.0,
        direction="SELL",
        lots=1,
        lot_size=20,
        premium=110.0,
        product="INTRADAY",
    )


def _order(status: str, avg: float = 0.0, filled: int = 0, reason: str | None = None) -> Order:
    return Order(
        broker_order_id="O1",
        symbol="INDEX26JAN20000PE",
        direction="BUY",
        quantity=20,
        filled_quantity=filled,
        pending_quantity=20 - filled,
        status=status,
        order_type="MARKET",
        product="INTRADAY",
        average_price=avg,
        tag="",
        placed_at=datetime.now(UTC),
        rejection_reason=reason,
    )


def _live_broker(*statuses: Order) -> MagicMock:
    b = MagicMock(spec=BrokerInterface)
    b.place_order = AsyncMock(return_value="O1")
    b.get_ltp = AsyncMock(return_value={"BFO:INDEX26JAN20000PE": 170.0})
    b.get_order_status = AsyncMock(side_effect=list(statuses))
    return b


def _exec(b: MagicMock, timeout: float = 0.05) -> OptionsLegExecutor:
    return OptionsLegExecutor(b, poll_interval_seconds=0.001, fill_timeout_seconds=timeout)


async def test_entry_premium_becomes_the_real_fill() -> None:
    leg = _leg()
    ex = _exec(_live_broker(_order("FILLED", 109.0, 20)))
    await ex._poll_until_filled([LegFillState(leg=leg, order_id="O1")], 0.05)
    assert leg.premium == 109.0


async def test_close_returns_the_real_fill_not_the_quote() -> None:
    ex = _exec(_live_broker(_order("OPEN"), _order("FILLED", 172.0, 20)))
    assert await ex.close_leg(_leg(), "POS") == 172.0


async def test_refused_close_raises_so_the_leg_stays_open_and_is_retried() -> None:
    ex = _exec(_live_broker(_order("REJECTED", reason="margin")))
    with pytest.raises(RuntimeError, match="margin"):
        await ex.close_leg(_leg(), "POS")


async def test_unreadable_fill_completes_with_the_quote_marked_as_estimate() -> None:
    b = _live_broker()
    b.get_order_status = AsyncMock(side_effect=RuntimeError("timeout"))
    ex = _exec(b)
    leg = _leg()
    assert await ex.close_leg(leg, "POS") == 170.0
    b.place_order.assert_awaited_once()
    assert leg.tradingsymbol in ex.estimated_closes


async def test_paper_keeps_its_modelled_price() -> None:
    b = MagicMock(spec=BrokerInterface)
    b.hint_prices = MagicMock()  # only paper brokers have this
    b.place_order = AsyncMock(return_value="O1")
    b.get_ltp = AsyncMock(return_value={"BFO:INDEX26JAN20000PE": 295.0})
    b.get_order_status = AsyncMock(return_value=_order("FILLED", 111.0, 20))
    ex = _exec(b)
    leg = _leg()
    assert await ex.close_leg(leg, "POS") == 295.0
    await ex._poll_until_filled([LegFillState(leg=leg, order_id="O1")], 0.05)
    assert leg.premium == 110.0
