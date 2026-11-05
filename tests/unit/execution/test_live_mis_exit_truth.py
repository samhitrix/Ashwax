"""Live MIS safety (2026-11-05): a filled position is never written off, every position keeps a
broker stop, an exit never double-sells, and a close is booked at the broker's real fill."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface, Order, OrderRequest, Position
from src.execution import order_lifecycle as ol_mod
from src.execution.order_lifecycle import (
    MonitoredPosition,
    OrderLifecycleManager,
    OrderMonitor,
    OrderState,
)


def _order(
    oid: str,
    status: str = "OPEN",
    filled: int = 0,
    avg: float = 0.0,
    qty: int = 10,
    direction: str = "SELL",
) -> Order:
    return Order(
        broker_order_id=oid,
        symbol="INFY",
        direction=direction,
        quantity=qty,
        filled_quantity=filled,
        pending_quantity=qty - filled,
        status=status,
        order_type="SL-M",
        product="INTRADAY",
        average_price=avg,
        tag="",
        placed_at=datetime.now(UTC),
    )


class FakeBroker:
    """Order book + net position; cancels follow broker rules (a filled order can't be cancelled)."""

    def __init__(self, held: int = 10) -> None:
        self.orders: dict[str, Order] = {}
        self.placed: list[OrderRequest] = []
        self.held = held
        self.fill_new_at: float | None = 1490.0  # new exit orders fill immediately at this price
        self.refuse_stop = False
        self.status_error = False
        self._next = 0
        b = MagicMock(spec=BrokerInterface)
        b.place_order = AsyncMock(side_effect=self._place)
        b.cancel_order = AsyncMock(side_effect=self._cancel)
        b.get_order_status = AsyncMock(side_effect=self._status)
        b.get_all_orders = AsyncMock(side_effect=lambda: list(self.orders.values()))
        b.get_positions = AsyncMock(side_effect=self._positions)
        b.get_ltp = AsyncMock(return_value={"INFY": 1500.0})
        b.modify_order = AsyncMock(return_value="ok")
        b.supports_gtt = MagicMock(return_value=True)
        self.mock = b

    def add(self, order: Order) -> str:
        self.orders[order.broker_order_id] = order
        return order.broker_order_id

    async def _place(self, req: OrderRequest) -> str:
        if self.refuse_stop and req.order_type == "SL-M":
            raise RuntimeError("stop refused")
        self.placed.append(req)
        self._next += 1
        oid = f"NEW{self._next}"
        if req.order_type == "SL-M" or self.fill_new_at is None:
            self.orders[oid] = _order(oid, "OPEN", qty=req.quantity)
        else:
            self.orders[oid] = _order(
                oid, "FILLED", filled=req.quantity, avg=self.fill_new_at, qty=req.quantity
            )
        return oid

    async def _cancel(self, oid: str) -> bool:
        o = self.orders[oid]
        if o.status == "FILLED":
            raise RuntimeError("order already complete")
        o.status = "CANCELLED"
        return True

    async def _status(self, oid: str) -> Order:
        if self.status_error:
            raise RuntimeError("broker unreachable")
        return self.orders[oid]

    async def _positions(self) -> list[Position]:
        if not self.held:
            return []
        return [Position("INFY", "NSE", "long", self.held, 1500.0, 1500.0, 0.0, "INTRADAY")]

    def exits(self) -> list[OrderRequest]:
        """Exit-side orders that would actually take stock out, ignoring stops."""
        return [r for r in self.placed if r.direction == "SELL" and r.order_type != "SL-M"]


def _pos(**kw: Any) -> MonitoredPosition:
    base: dict[str, Any] = dict(
        agent_trade_id="T1",
        symbol="INFY",
        direction="BUY",
        quantity=10,
        fill_price=1500.0,
        initial_sl=1480.0,
        current_sl=1480.0,
        initial_target=1550.0,
        current_target=1550.0,
        product="MIS",
    )
    base.update(kw)
    return MonitoredPosition(**base)


def _monitor(fb: FakeBroker, pos: MonitoredPosition | None = None) -> OrderMonitor:
    mon = OrderMonitor(fb.mock, is_paper=False, mis_flat_by="23:59", trade_repo=AsyncMock())
    if pos is not None:
        mon.track(pos)
    return mon


def _protected(fb: FakeBroker, **kw: Any) -> MonitoredPosition:
    sl = fb.add(_order("SL1"))
    tg = fb.add(_order("TG1"))
    return _pos(sl_order_id=sl, target_order_id=tg, **kw)


EXIT_PATHS = [
    lambda m, p: m._exec_mis_force_exit(p),
    lambda m, p: m._exec_sl_gap_breach(p),
    lambda m, p: m._exec_target_limit_missed(p),
    lambda m, p: m.force_exit(p.agent_trade_id, 1500.0, "REVERSAL"),
]


# ── Entry: a filled position is never written off ─────────────────────────────


def _entry_broker(entry: Order, refuse: tuple[str, ...]) -> MagicMock:
    b = MagicMock(spec=BrokerInterface)
    ids = iter(["E1", "S1", "G1"])

    async def place(req: OrderRequest) -> str:
        if req.order_type in refuse:
            raise RuntimeError("refused by broker")
        return next(ids)

    b.place_order = AsyncMock(side_effect=place)
    b.cancel_order = AsyncMock(side_effect=RuntimeError("order complete"))
    b.get_order_status = AsyncMock(return_value=entry)
    b.get_trades = AsyncMock(return_value=[MagicMock(quantity=10, price=1500.0)])
    b.supports_gtt = MagicMock(return_value=True)
    return b


async def test_filled_entry_with_refused_stop_is_not_rejected(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(ol_mod, "notify", lambda kind, **kw: calls.append((kind, kw)))
    b = _entry_broker(_order("E1", "FILLED", filled=10, avg=1500.0, direction="BUY"), ("SL-M",))
    mgr = OrderLifecycleManager(b, is_paper=False)
    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1480.0,
        target_price=1550.0,
        product="MIS",
    )
    assert rec.state == OrderState.FILLED
    assert rec.fill_price == 1500.0
    assert rec.sl_order_id == ""
    kinds = [c[0] for c in calls]
    assert ol_mod.EventKind.ENTRY_REJECTED not in kinds
    assert ol_mod.EventKind.STRATEGY_ERROR in kinds  # "position open, no broker stop"


async def test_unfilled_cancelled_entry_with_refused_stop_still_rejects() -> None:
    b = _entry_broker(_order("E1", "CANCELLED", direction="BUY"), ("SL-M",))
    b.cancel_order = AsyncMock(return_value=True)
    mgr = OrderLifecycleManager(b, is_paper=False)
    with pytest.raises(RuntimeError, match="refused"):
        await mgr.place_entry_protection_and_poll(
            signal_id="SIG",
            symbol="INFY",
            direction="BUY",
            qty=10,
            entry_price=1500.0,
            sl_price=1480.0,
            target_price=1550.0,
            product="MIS",
        )


async def test_unreadable_entry_status_is_treated_as_a_real_position() -> None:
    b = _entry_broker(_order("E1", "FILLED", filled=10, avg=1500.0, direction="BUY"), ("SL-M",))
    b.get_order_status = AsyncMock(
        side_effect=[RuntimeError("timeout"), _order("E1", "FILLED", filled=10, avg=1500.0)]
    )
    mgr = OrderLifecycleManager(b, is_paper=False)
    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1480.0,
        target_price=1550.0,
        product="MIS",
    )
    assert rec.state == OrderState.FILLED


async def test_refused_target_keeps_the_stop() -> None:
    b = _entry_broker(_order("E1", "FILLED", filled=10, avg=1500.0, direction="BUY"), ("LIMITT",))

    async def place(req: OrderRequest) -> str:
        if req.agent_trade_id.endswith("-TGT"):
            raise RuntimeError("target refused")
        return "S1" if req.agent_trade_id.endswith("-SL") else "E1"

    b.place_order = AsyncMock(side_effect=place)
    mgr = OrderLifecycleManager(b, is_paper=False)
    rec = await mgr.place_entry_protection_and_poll(
        signal_id="SIG",
        symbol="INFY",
        direction="BUY",
        qty=10,
        entry_price=1500.0,
        sl_price=1480.0,
        target_price=1550.0,
        product="MIS",
    )
    assert rec.sl_order_id == "S1"
    assert rec.target_order_id == ""
    assert rec.state == OrderState.FILLED


def test_mis_stop_config_is_stop_market() -> None:
    cfg = json.loads(Path("config/execution.json").read_text(encoding="utf-8"))
    assert cfg["order_types"]["MIS_intraday"] == "SL-M"


# ── Exits can't double-sell ───────────────────────────────────────────────────


@pytest.mark.parametrize("path", EXIT_PATHS)
async def test_exit_cancels_resting_legs_first_and_books_real_fill(path) -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    mon = _monitor(fb, pos)
    await path(mon, pos)
    assert fb.orders["SL1"].status == "CANCELLED"
    assert fb.orders["TG1"].status == "CANCELLED"
    assert len(fb.exits()) == 1
    assert mon.get("T1") is None
    assert mon.closed_trades[0]["exit_price"] == 1490.0  # broker fill, not LTP/limit


@pytest.mark.parametrize("path", EXIT_PATHS)
async def test_no_second_sell_when_stop_already_filled(path) -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.orders["SL1"] = _order("SL1", "FILLED", filled=10, avg=1478.5)
    mon = _monitor(fb, pos)
    await path(mon, pos)
    assert fb.exits() == []
    assert fb.orders["TG1"].status == "CANCELLED"
    assert mon.closed_trades[0]["exit_price"] == 1478.5
    assert mon.get("T1") is None


@pytest.mark.parametrize("path", EXIT_PATHS)
async def test_partial_target_fill_sells_only_the_remainder(path) -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.orders["TG1"] = _order("TG1", "OPEN", filled=4, avg=1550.0)
    mon = _monitor(fb, pos)
    await path(mon, pos)
    assert [r.quantity for r in fb.exits()] == [6]
    assert mon.closed_trades[0]["partial_qty_booked"] == 4


@pytest.mark.parametrize("path", EXIT_PATHS)
async def test_unreadable_status_places_nothing_and_keeps_tracking(path) -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.status_error = True
    mon = _monitor(fb, pos)
    await path(mon, pos)
    assert fb.exits() == []
    assert mon.get("T1") is pos
    assert mon.closed_trades == []


@pytest.mark.parametrize("path", EXIT_PATHS)
async def test_broker_already_flat_places_no_exit(path) -> None:
    """Manual close in Kite: selling again would open a naked short."""
    fb = FakeBroker(held=0)
    pos = _protected(fb)
    mon = _monitor(fb, pos)
    await path(mon, pos)
    assert fb.exits() == []


async def test_first_chase_order_settles_resting_legs() -> None:
    fb = FakeBroker()
    fb.fill_new_at = None
    pos = _protected(fb)
    mon = _monitor(fb, pos)
    await mon._advance_chase(pos, "SL", 1475.0, "SELL")
    assert fb.orders["SL1"].status == "CANCELLED"
    assert pos.chase_order_id


async def test_chase_filled_during_repeg_is_booked_not_resold() -> None:
    fb = FakeBroker()
    pos = _pos()
    pos.chase_order_id = fb.add(_order("C1", "FILLED", filled=10, avg=1474.0))
    pos.chase_reason = "SL"
    pos.chase_attempts = 1
    mon = _monitor(fb, pos)
    fb.mock.get_order_status = AsyncMock(
        side_effect=[_order("C1", "OPEN"), _order("C1", "FILLED", filled=10, avg=1474.0)]
    )
    await mon._advance_chase(pos, "SL", 1470.0, "SELL")
    assert fb.exits() == []
    assert mon.closed_trades[0]["exit_price"] == 1474.0


# ── Closed means the broker says filled ───────────────────────────────────────


async def test_unfilled_exit_limit_stays_tracked_then_goes_market() -> None:
    fb = FakeBroker()
    fb.fill_new_at = None
    pos = _pos(sl_order_id=fb.add(_order("SL1")))
    mon = _monitor(fb, pos)
    await mon._exec_sl_gap_breach(pos)
    assert mon.get("T1") is pos
    assert mon.closed_trades == []
    oid = pos.exit_order_id
    assert oid

    for _ in range(mon._chase_max_attempts - 1):
        await mon._check_broker_exit_orders()
    assert fb.orders[oid].status == "CANCELLED"

    fb.fill_new_at = 1469.0
    await mon._check_broker_exit_orders()
    assert fb.placed[-1].order_type == "MARKET"
    assert mon.get("T1") is None
    assert mon.closed_trades[0]["exit_price"] == 1469.0


async def test_rejected_exit_is_never_booked_closed() -> None:
    fb = FakeBroker()
    fb.fill_new_at = None
    pos = _pos(sl_order_id=fb.add(_order("SL1")))
    mon = _monitor(fb, pos)
    await mon._exec_mis_force_exit(pos)
    fb.orders[pos.exit_order_id].status = "REJECTED"
    await mon._check_broker_exit_orders()
    assert mon.get("T1") is pos
    assert mon.closed_trades == []


async def test_failed_exit_placement_is_not_booked_closed() -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    mon = _monitor(fb, pos)
    fb.mock.place_order = AsyncMock(side_effect=RuntimeError("down"))
    await mon.force_exit("T1", 1500.0, "REVERSAL")
    assert mon.get("T1") is pos
    assert mon.closed_trades == []


async def test_paper_exit_books_immediately_as_before() -> None:
    fb = FakeBroker()
    pos = _pos(sl_order_id="PAPER-SL")
    mon = OrderMonitor(fb.mock, is_paper=True, mis_flat_by="23:59")
    mon.track(pos)
    await mon._exec_mis_force_exit(pos)
    assert mon.get("T1") is None
    assert len(mon.closed_trades) == 1


# ── Continuous broker check ───────────────────────────────────────────────────


async def test_stop_filled_at_broker_cancels_target_and_books_real_price() -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.orders["SL1"] = _order("SL1", "FILLED", filled=10, avg=1478.5)
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    assert fb.orders["TG1"].status == "CANCELLED"
    assert mon.get("T1") is None
    assert mon.closed_trades[0]["exit_price"] == 1478.5
    assert fb.exits() == []


async def test_target_filled_at_broker_cancels_stop() -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.orders["TG1"] = _order("TG1", "FILLED", filled=10, avg=1551.0)
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    assert fb.orders["SL1"].status == "CANCELLED"
    assert mon.closed_trades[0]["exit_price"] == 1551.0


async def test_missing_stop_is_placed_and_persisted_once() -> None:
    fb = FakeBroker()
    pos = _pos()
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    await mon._check_broker_exit_orders()
    stops = [r for r in fb.placed if r.order_type == "SL-M"]
    assert len(stops) == 1
    assert stops[0].trigger_price == 1480.0
    assert stops[0].quantity == 10
    assert pos.sl_order_id == "NEW1"
    mon._trade_repo.update_state.assert_awaited()
    assert mon._trade_repo.update_state.await_args.kwargs["sl_order_id"] == "NEW1"


async def test_stop_rejected_after_acceptance_is_replaced_once() -> None:
    fb = FakeBroker()
    pos = _pos(sl_order_id=fb.add(_order("SL1", "REJECTED")))
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    await mon._check_broker_exit_orders()
    assert len([r for r in fb.placed if r.order_type == "SL-M"]) == 1


async def test_stop_is_not_duplicated_after_restart() -> None:
    """Reseeded from the DB with the persisted id: the resting stop is recognised."""
    fb = FakeBroker()
    pos = _pos(sl_order_id=fb.add(_order("SL1")))
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    assert fb.placed == []


async def test_stop_id_absent_from_book_alerts_but_never_replaces(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(ol_mod, "notify", lambda kind, **kw: calls.append(kind))
    fb = FakeBroker()
    pos = _pos(sl_order_id="UNKNOWN")
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    assert fb.placed == []
    assert ol_mod.EventKind.STRATEGY_ERROR in calls


async def test_refused_stop_retry_is_spaced_and_alerts_once(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(ol_mod, "notify", lambda kind, **kw: calls.append(kind))
    fb = FakeBroker()
    fb.refuse_stop = True
    pos = _pos()
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    await mon._check_broker_exit_orders()
    assert fb.mock.place_order.await_count == 1
    assert calls.count(ol_mod.EventKind.STRATEGY_ERROR) == 1


async def test_no_stop_placed_when_broker_is_flat() -> None:
    fb = FakeBroker(held=0)
    pos = _pos()
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    assert fb.placed == []


async def test_stop_resized_after_partial_target_fill() -> None:
    fb = FakeBroker()
    pos = _protected(fb)
    fb.orders["TG1"] = _order("TG1", "OPEN", filled=4, avg=1550.0)
    mon = _monitor(fb, pos)
    await mon._check_broker_exit_orders()
    fb.mock.modify_order.assert_awaited_once_with("SL1", {"quantity": 6})


async def test_paper_monitor_never_reads_the_order_book() -> None:
    fb = FakeBroker()
    mon = OrderMonitor(fb.mock, is_paper=True, mis_flat_by="23:59")
    mon.track(_pos())
    await mon._check_broker_exit_orders()
    fb.mock.get_all_orders.assert_not_awaited()
    assert fb.placed == []


async def test_software_stop_still_runs_while_broker_stop_refused() -> None:
    fb = FakeBroker()
    fb.refuse_stop = True
    fb.mock.get_ltp = AsyncMock(return_value={"INFY": 1470.0})
    pos = _pos()
    mon = _monitor(fb, pos)
    await mon.cycle()
    assert pos.chase_order_id or mon.get("T1") is None
