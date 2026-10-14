"""Tests for orchestrator._reseed_order_monitor (2026-08-17 fix).

Root cause: OrderMonitor.track() is only ever called at the moment of entry.
Its _positions dict is in-memory only, so every process restart silently
dropped SL/target monitoring for positions that were already open — found via
14 open swing trades showing "Orders Watched: 0" on the dashboard despite
having live SL/target prices in the DB.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args):
        return self._rows


def _row(**overrides):
    row = {
        "agent_trade_id": "TRD-SCI",
        "symbol": "NSE:SCI",
        "direction": "long",
        "quantity": 40,
        "fill_price": 308.40,
        "initial_sl": 283.73,
        "current_sl": 283.73,
        "initial_target": 370.08,
        "current_target": 370.08,
        "sl_order_id": None,
        "target_order_id": None,
        "gtt_sl_id": None,
        "gtt_target_id": None,
        "product": "CNC",
        "entry_timestamp": datetime(2026, 8, 7, 15, 47, 42, tzinfo=UTC),
        "strategy_name": "orb_intraday_smart",
        "partial_pnl_booked": 0,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_tracks_open_db_trade_not_in_memory() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row()])
    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 1
    tracked = mon.get_monitored()
    assert len(tracked) == 1
    pos = tracked[0]
    assert pos.symbol == "NSE:SCI"
    assert pos.direction == "BUY"  # mapped from DB's 'long'
    assert pos.current_sl == 283.73
    assert pos.product == "CNC"
    assert pos.strategy_name == "orb_intraday_smart"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_maps_short_direction() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row(direction="short")])
    mon = OrderMonitor(MagicMock())
    await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert mon.get_monitored()[0].direction == "SELL"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_is_idempotent_skips_already_tracked() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor

    pool = _FakePool([_row()])
    mon = OrderMonitor(MagicMock())
    mon.track(
        MonitoredPosition(
            agent_trade_id="TRD-SCI",
            symbol="NSE:SCI",
            direction="BUY",
            quantity=40,
            fill_price=308.40,
            initial_sl=283.73,
            current_sl=283.73,
            initial_target=370.08,
            current_target=370.08,
        )
    )
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 0
    assert len(mon.get_monitored()) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_returns_zero_when_pool_is_none() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, None, paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_skips_rows_missing_agent_trade_id() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    pool = _FakePool([_row(agent_trade_id=None)])
    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, pool, paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reseed_survives_fetch_error() -> None:
    from src import orchestrator
    from src.execution.order_lifecycle import OrderMonitor

    class _BrokenPool:
        async def fetch(self, query, *args):
            raise RuntimeError("connection lost")

    mon = OrderMonitor(MagicMock())
    added = await orchestrator._reseed_order_monitor(mon, _BrokenPool(), paper_mode=True)

    assert added == 0
    assert mon.get_monitored() == []
