"""Behavioral tests proving the Kite adapter does NOT block the asyncio event loop.

Root cause of the production "agent status flickers STOPPED every second" bug:
every KiteAdapter method was `async def` but called the synchronous, blocking
kiteconnect client directly on the event loop. A slow Kite HTTP round-trip froze
the whole single-process orchestrator, starving the 10 s heartbeat → status aged
past the RUNNING threshold → flicker.

These tests inject a fake `_kite` whose calls genuinely block (time.sleep) and
assert that a concurrent ticker task keeps advancing — i.e. the loop stayed
responsive. This is a real concurrency behavior test, not a mock of the result.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest
from src.brokers.adapters.kite.adapter import KiteAdapter
from src.brokers.adapters.kite.auth import KiteAuth


def _make_adapter() -> KiteAdapter:
    auth = KiteAuth(api_key="test_key", api_secret="test_secret", user_id="AB1234")
    return KiteAdapter(auth)


_POSITIONS_RAW = {
    "net": [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "quantity": 10,
            "average_price": 2500.0,
            "last_price": 2550.0,
            "pnl": 500.0,
            "product": "MIS",
        }
    ],
    "day": [],
}


async def _count_ticks(stop: asyncio.Event) -> int:
    """Increment once every 20 ms until told to stop. If the event loop is
    blocked by a synchronous broker call, this task cannot run and the count
    stays low."""
    ticks = 0
    while not stop.is_set():
        await asyncio.sleep(0.02)
        ticks += 1
    return ticks


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_positions_does_not_block_event_loop() -> None:
    adapter = _make_adapter()

    def _blocking_positions() -> dict[str, object]:
        time.sleep(0.5)  # simulate a slow Kite HTTP round-trip
        return _POSITIONS_RAW

    stop = asyncio.Event()
    ticker = asyncio.create_task(_count_ticks(stop))
    # Yield so the ticker starts before the blocking call.
    await asyncio.sleep(0)

    with patch.object(adapter._kite, "positions", side_effect=_blocking_positions):
        positions = await adapter.get_positions()

    stop.set()
    ticks = await ticker

    assert len(positions) == 1
    assert positions[0].symbol == "RELIANCE"
    # During a 0.5 s blocking call, a 20 ms ticker would advance ~25 times if the
    # loop stayed responsive. If the call blocked the loop, ticks would be ~0-1.
    assert ticks >= 10, f"event loop appears blocked during broker call (ticks={ticks})"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_get_margins_does_not_block_event_loop() -> None:
    adapter = _make_adapter()

    def _blocking_margins() -> dict[str, object]:
        time.sleep(0.5)
        return {"equity": {"net": 100000.0, "available": {"cash": 100000.0}}}

    stop = asyncio.Event()
    ticker = asyncio.create_task(_count_ticks(stop))
    await asyncio.sleep(0)

    with patch.object(adapter._kite, "margins", side_effect=_blocking_margins):
        margin = await adapter.get_margins()

    stop.set()
    ticks = await ticker

    assert margin.equity == 100000.0
    assert ticks >= 10, f"event loop appears blocked during broker call (ticks={ticks})"
