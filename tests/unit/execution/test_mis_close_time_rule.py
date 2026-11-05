"""Rule (2026-11-05): every MIS position closes at close time, live or paper, whoever opened it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.brokers.base import BrokerInterface, Order, OrderRequest, Position
from src.execution import order_lifecycle as ol_mod
from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor


def _position(symbol: str = "MANUAL", qty: int = 10, side: str = "long", product: str = "INTRADAY"):
    return Position(symbol, "NSE", side, qty, 100.0, 100.0, 0.0, product)


def _order(oid: str, symbol: str, side: str, status: str, placed_at=None) -> Order:
    return Order(
        broker_order_id=oid,
        symbol=symbol,
        direction=side,
        quantity=10,
        filled_quantity=10 if status == "FILLED" else 0,
        pending_quantity=0,
        status=status,
        order_type="LIMIT",
        product="INTRADAY",
        average_price=100.0,
        tag="",
        placed_at=placed_at or datetime.now(UTC) - timedelta(hours=1),
    )


def _broker(positions: list[Position], orders: list[Order] | None = None) -> MagicMock:
    b = MagicMock(spec=BrokerInterface)
    b.get_positions = AsyncMock(return_value=positions)
    b.get_all_orders = AsyncMock(return_value=orders or [])
    b.place_order = AsyncMock(return_value="X1")
    b.get_ltp = AsyncMock(return_value={})
    book = {o.broker_order_id: o for o in orders or []}

    async def cancel(oid: str) -> bool:
        book[oid].status = "CANCELLED"
        return True

    async def status(oid: str) -> Order:
        return book[oid]

    b.cancel_order = AsyncMock(side_effect=cancel)
    b.get_order_status = AsyncMock(side_effect=status)
    return b


def _monitor(b: MagicMock, flat: str = "00:00", is_paper: bool = False) -> OrderMonitor:
    return OrderMonitor(b, is_paper=is_paper, mis_flat_by=flat)


def _tracked(symbol: str = "MINE") -> MonitoredPosition:
    return MonitoredPosition(
        agent_trade_id="T1",
        symbol=symbol,
        direction="BUY",
        quantity=10,
        fill_price=100.0,
        initial_sl=90.0,
        current_sl=90.0,
        initial_target=120.0,
        current_target=120.0,
        product="MIS",
    )


async def test_manual_mis_position_is_closed_after_close_time(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(ol_mod, "notify", lambda kind, **kw: calls.append(kw))
    b = _broker([_position()])
    await _monitor(b).flatten_untracked_mis(in_session=True)
    req: OrderRequest = b.place_order.await_args.args[0]
    assert (req.symbol, req.direction, req.quantity) == ("MANUAL", "SELL", 10)
    assert req.order_type == "MARKET"
    assert req.product == "INTRADAY"
    assert "MANUAL" in str(calls[0])


async def test_short_position_closes_by_buying_on_its_own_exchange() -> None:
    p = Position("SENSEX1CE", "BFO", "short", 20, 100.0, 100.0, 0.0, "INTRADAY")
    b = _broker([p])
    await _monitor(b).flatten_untracked_mis(in_session=True)
    req = b.place_order.await_args.args[0]
    assert (req.direction, req.exchange, req.quantity) == ("BUY", "BFO", 20)


async def test_nothing_happens_before_close_time() -> None:
    b = _broker([_position()])
    await _monitor(b, flat="23:59").flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_nothing_is_sent_outside_the_market_session() -> None:
    b = _broker([_position()])
    await _monitor(b).flatten_untracked_mis(in_session=False)
    b.place_order.assert_not_awaited()


async def test_delivery_and_nrml_are_never_touched() -> None:
    b = _broker([_position("HOLD", product="DELIVERY"), _position("FUT", product="MARGIN")])
    await _monitor(b).flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_tracked_position_is_left_to_its_own_exit() -> None:
    b = _broker([_position("MINE")])
    mon = _monitor(b)
    mon.track(_tracked("MINE"))
    await mon.flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_desk_legs_are_exempt_until_their_own_exit_time() -> None:
    b = _broker([_position("SENSEX1CE")])
    await _monitor(b).flatten_untracked_mis(in_session=True, exempt={"SENSEX1CE"})
    b.place_order.assert_not_awaited()


async def test_resting_exit_orders_are_cancelled_first() -> None:
    resting = _order("R1", "MANUAL", "SELL", "OPEN")
    b = _broker([_position()], [resting])
    await _monitor(b).flatten_untracked_mis(in_session=True)
    b.cancel_order.assert_awaited_once_with("R1")
    b.place_order.assert_awaited_once()


async def test_exit_is_not_sent_when_a_resting_order_cannot_be_cancelled() -> None:
    resting = _order("R1", "MANUAL", "SELL", "OPEN")
    b = _broker([_position()], [resting])
    b.cancel_order = AsyncMock(side_effect=RuntimeError("refused"))
    await _monitor(b).flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_recent_exit_fill_blocks_a_second_sell_on_a_stale_position_read() -> None:
    fresh = _order("F1", "MANUAL", "SELL", "FILLED", placed_at=datetime.now(UTC))
    b = _broker([_position()], [fresh])
    await _monitor(b).flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_open_flatten_order_is_not_resent() -> None:
    b = _broker([_position()], [_order("X1", "MANUAL", "SELL", "OPEN")])
    mon = _monitor(b)
    mon._flatten_orders["MANUAL"] = "X1"
    await mon.flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_refused_flatten_order_is_retried_next_cycle() -> None:
    b = _broker([_position()])
    b.place_order = AsyncMock(side_effect=[RuntimeError("no"), "X2"])
    mon = _monitor(b)
    await mon.flatten_untracked_mis(in_session=True)
    await mon.flatten_untracked_mis(in_session=True)
    assert b.place_order.await_count == 2


async def test_paper_monitor_has_no_broker_positions_to_sweep() -> None:
    b = _broker([_position()])
    await _monitor(b, is_paper=True).flatten_untracked_mis(in_session=True)
    b.place_order.assert_not_awaited()


async def test_flat_by_fires_with_no_live_price() -> None:
    """A missing quote skipped the flat-by; the position then rode past close time."""
    b = _broker([_position("MINE")])
    b.get_order_status = AsyncMock(return_value=_order("X1", "MINE", "SELL", "FILLED"))
    mon = _monitor(b)
    mon.track(_tracked("MINE"))
    events = await mon.cycle()
    assert [e["type"] for e in events] == ["MIS_FORCE_EXIT"]


async def test_flat_by_fires_when_the_quote_call_fails() -> None:
    b = _broker([_position("MINE")])
    b.get_ltp = AsyncMock(side_effect=RuntimeError("down"))
    mon = _monitor(b)
    mon.track(_tracked("MINE"))
    events = await mon.cycle()
    assert [e["type"] for e in events] == ["MIS_FORCE_EXIT"]


async def test_paper_mis_position_flat_by_also_fires_with_no_price() -> None:
    b = _broker([])
    mon = _monitor(b, is_paper=True)
    mon.track(_tracked("MINE"))
    events = await mon.cycle()
    assert [e["type"] for e in events] == ["MIS_FORCE_EXIT"]
    await mon.execute_events(events)
    assert mon.get("T1") is None
