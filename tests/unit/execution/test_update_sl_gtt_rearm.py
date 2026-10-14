"""update_sl re-arms the protection GTT at the NEW stop when the old GTT no longer
exists (paper GTTs die on restart) instead of leaving it unprotected."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.brokers.base import BrokerInterface
from src.execution.order_lifecycle import MonitoredPosition, OrderMonitor


def _pos() -> MonitoredPosition:
    return MonitoredPosition(
        agent_trade_id="TRD1",
        symbol="LAURUSLABS",
        direction="long",
        quantity=26,
        fill_price=1905.5,
        initial_sl=1828.58,
        current_sl=1828.58,
        initial_target=2053.8,
        current_target=2053.8,
        gtt_sl_id="PAPER-GTT-000004",
        gtt_target_id="PAPER-GTT-000004",
        product="CNC",
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_missing_gtt_is_rearmed_at_new_stop() -> None:
    broker = MagicMock(spec=BrokerInterface)
    broker.modify_gtt = AsyncMock(side_effect=KeyError("not found"))
    broker.supports_gtt = MagicMock(return_value=True)
    broker.place_gtt = AsyncMock(return_value="PAPER-GTT-000009")
    mon = OrderMonitor(broker)
    mon.track(_pos())
    await mon.update_sl("TRD1", 1905.5)
    placed = broker.place_gtt.await_args.args[0]
    assert placed.sl_trigger == 1905.5 and placed.target_trigger == 2053.8
    assert mon.get("TRD1").gtt_sl_id == "PAPER-GTT-000009"
