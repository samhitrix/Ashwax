"""Tests for the 'never trade a symbol already in a position' guard.

Regression: orb_intraday_top opened BPCL/DRREDDY (2026-06-30) and the daily
path had no per-symbol dedup at all. The intraday entry guard must refuse a new
trade on a symbol already held — whether engine-tracked (order_monitor) or an
external/broker holding cached in shared['held_symbols'] — no matter what.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from src.orchestrator import _execute_intraday_signal

_NOW = datetime(2026, 6, 30, 9, 31)
# Guard returns before sig is dereferenced; provide attrs anyway for safety.
_SIG = SimpleNamespace(
    entry_price=100.0, sl=98.0, target=104.0, direction=SimpleNamespace(value="LONG")
)


def _monitor(symbols: list[str]) -> SimpleNamespace:
    return SimpleNamespace(get_monitored=lambda: [SimpleNamespace(symbol=s) for s in symbols])


# held_symbols_loaded=True: the held set has been loaded from the DB/broker at least
# once (first reconciliation), so the guard evaluates the held check rather than
# failing closed. See test_fails_closed_before_first_reconciliation for the boot case.


@pytest.mark.asyncio  # type: ignore[misc]
async def test_blocks_when_held_externally() -> None:
    shared = {
        "paper_mode": True,
        "held_symbols_loaded": True,
        "held_symbols": {"BPCL"},
        "order_monitor": _monitor([]),
    }
    assert await _execute_intraday_signal(shared, "orb_intraday_top", "BPCL", _SIG, _NOW) is None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_blocks_when_engine_already_holds() -> None:
    shared = {
        "paper_mode": True,
        "held_symbols_loaded": True,
        "order_monitor": _monitor(["DRREDDY"]),
    }
    assert await _execute_intraday_signal(shared, "orb_intraday_top", "DRREDDY", _SIG, _NOW) is None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_symbol_normalized_against_exchange_prefix() -> None:
    # order_monitor stores "NSE:BPCL"; the bare signal symbol must still match.
    shared = {
        "paper_mode": True,
        "held_symbols_loaded": True,
        "order_monitor": _monitor(["NSE:BPCL"]),
    }
    assert await _execute_intraday_signal(shared, "orb_intraday_top", "BPCL", _SIG, _NOW) is None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_fails_closed_before_first_reconciliation() -> None:
    """Restart-window regression: with the held set not yet loaded from the DB/broker,
    the guard must REFUSE even a symbol it is not tracking — otherwise a launcher
    restart re-enters an already-open position (observed: repeated CIPLA SHORTs)."""
    shared = {"paper_mode": True, "order_monitor": _monitor([])}  # no held_symbols_loaded
    assert await _execute_intraday_signal(shared, "orb_intraday_v3", "CIPLA", _SIG, _NOW) is None
