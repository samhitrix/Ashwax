"""Regression net for the hard constraint: a notification failure must never
fail, delay, or block a trade. Monkeypatches src.execution.order_lifecycle.notify
(the name imported into that module) with a recorder / a raiser, reusing the
existing fakes from test_order_lifecycle.py.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import src.execution.order_lifecycle as ol_mod
from src.brokers.base import BrokerInterface, Fill, Order, OrderRequest
from src.execution.order_lifecycle import (
    MonitoredPosition,
    OrderLifecycleManager,
    OrderMonitor,
    OrderState,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


def _order_req(agent_trade_id: str = "AT001") -> OrderRequest:
    return OrderRequest(
        symbol="INFY",
        exchange="NSE",
        direction="BUY",
        quantity=10,
        order_type="MARKET",
        product="INTRADAY",
        validity="DAY",
        agent_trade_id=agent_trade_id,
        signal_id="SIG001",
    )


def _filled_broker(fill_price: float = 1500.0) -> MagicMock:
    """Broker whose entry/SL/target placement reports a synchronous FILLED —
    mirrors tests/unit/execution/test_order_lifecycle.py's own _filled_broker."""
    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(side_effect=["E001", "SL001", "TGT001"])
    broker.get_order_status = AsyncMock(
        return_value=Order(
            broker_order_id="E001",
            symbol="INFY",
            direction="BUY",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            status="FILLED",
            order_type="MARKET",
            product="INTRADAY",
            average_price=fill_price,
            tag="",
            placed_at=_NOW,
        )
    )
    broker.get_trades = AsyncMock(
        return_value=[
            Fill(
                order_id="E001",
                trade_id="TRADE001",
                quantity=10,
                price=fill_price,
                exchange_time=_NOW,
            )
        ]
    )
    broker.modify_order = AsyncMock(return_value="OK")
    broker.modify_gtt = AsyncMock(return_value="GTT2")
    return broker


def _monitored(tid: str = "TRD001") -> MonitoredPosition:
    return MonitoredPosition(
        agent_trade_id=tid,
        symbol="INFY",
        direction="BUY",
        quantity=10,
        fill_price=1500.0,
        initial_sl=1450.0,
        current_sl=1450.0,
        initial_target=1600.0,
        current_target=1600.0,
        sl_order_id="SL001",
        target_order_id="TGT001",
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, kind, **kwargs):  # matches notify()'s signature loosely
        self.calls.append((kind, kwargs))


def _raiser(*_args, **_kwargs):
    raise RuntimeError("notification subsystem exploded")


# ── entry fill emits exactly one event, and survives notify() raising ──────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_entry_fill_emits_one_event(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(ol_mod, "notify", recorder)

    broker = _filled_broker(1505.0)
    mgr = OrderLifecycleManager(broker, is_paper=True)
    record = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1480.0,
        target_price=1550.0,
        product="MIS",
    )

    assert record.state == OrderState.FILLED
    entry_calls = [c for c in recorder.calls if c[0] == ol_mod.EventKind.ENTRY_FILL]
    assert len(entry_calls) == 1
    assert entry_calls[0][1]["symbol"] == "INFY"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_entry_fill_completes_when_notify_raises(monkeypatch) -> None:
    """The critical test: a broken notifier must never prevent a fill from
    being recorded. notify() raising must not propagate."""
    monkeypatch.setattr(ol_mod, "notify", _raiser)

    broker = _filled_broker(1505.0)
    mgr = OrderLifecycleManager(broker, is_paper=True)
    record = await mgr.place_entry_protection_and_poll(
        signal_id="SIG1",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1480.0,
        target_price=1550.0,
        product="MIS",
    )

    # The trade fill itself must be entirely unaffected.
    assert record.state == OrderState.FILLED
    assert record.fill_price == 1505.0
    assert record.filled_quantity == 10


# ── update_sl emits a modify event, and survives notify() raising ─────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_update_sl_emits_modify_event(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(ol_mod, "notify", recorder)

    broker = MagicMock(spec=BrokerInterface)
    broker.modify_order = AsyncMock(return_value=True)
    mon = OrderMonitor(broker)
    mon.track(_monitored())
    await mon.update_sl("TRD001", 1475.0)

    modify_calls = [c for c in recorder.calls if c[0] == ol_mod.EventKind.MODIFY_SL]
    assert len(modify_calls) == 1
    assert modify_calls[0][1]["new_sl"] == 1475.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_update_sl_completes_when_notify_raises(monkeypatch) -> None:
    """The critical test: patch notify() to raise, assert pos.current_sl is
    still updated AND the broker modify still happened. This proves a
    notification failure can never fail a trade."""
    monkeypatch.setattr(ol_mod, "notify", _raiser)

    broker = MagicMock(spec=BrokerInterface)
    broker.modify_order = AsyncMock(return_value=True)
    mon = OrderMonitor(broker)
    pos = _monitored()
    mon.track(pos)

    await mon.update_sl("TRD001", 1475.0)  # must not raise

    assert pos.current_sl == 1475.0
    broker.modify_order.assert_awaited_once()


# ── book_partial emits a partial event, and survives notify() raising ─────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_book_partial_emits_partial_event(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(ol_mod, "notify", recorder)

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="PARTIAL1")
    mon = OrderMonitor(broker)
    mon.track(_monitored())
    await mon.book_partial("TRD001", 0.5, 1520.0)

    partial_calls = [c for c in recorder.calls if c[0] == ol_mod.EventKind.PARTIAL_BOOK]
    assert len(partial_calls) == 1
    assert partial_calls[0][1]["booked_qty"] == 5


@pytest.mark.asyncio  # type: ignore[misc]
async def test_book_partial_completes_when_notify_raises(monkeypatch) -> None:
    monkeypatch.setattr(ol_mod, "notify", _raiser)

    broker = MagicMock(spec=BrokerInterface)
    broker.place_order = AsyncMock(return_value="PARTIAL1")
    mon = OrderMonitor(broker)
    pos = _monitored()
    mon.track(pos)

    await mon.book_partial("TRD001", 0.5, 1520.0)  # must not raise

    assert pos.quantity == 5  # the partial exit still went through
    broker.place_order.assert_awaited_once()
