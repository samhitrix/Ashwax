"""_persist_closed_trades must hand update_pnl the LAST-LEG net plus the partial
figures — update_pnl adds partial_pnl_booked in SQL, so passing the whole-trade
total here would count the partial twice (2026-09-24 LAURUSLABS)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from src.orchestrator import _persist_closed_trades


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.update_pnl = AsyncMock()
    repo.update_state = AsyncMock()
    return repo


@pytest.mark.asyncio  # type: ignore[misc]
async def test_persist_passes_last_leg_and_partial_figures() -> None:
    repo = _repo()
    rec = {
        "agent_trade_id": "TRD-1",
        "net_pnl": 3624.28,  # WHOLE trade
        "partial_pnl_booked": 1812.14,
        "partial_qty_booked": 13,
        "total_costs": 66.36,
        "exit_price": 2050.0,
        "exit_ts": "2026-09-24T12:07:12+05:30",
        "reason": "MANUAL_MARKET_EXIT",
    }
    await _persist_closed_trades(repo, [rec], is_paper=True)

    args, kwargs = repo.update_pnl.call_args
    assert args[1] == Decimal("1812.14")  # last leg only; SQL adds the partial
    assert kwargs["partial_pnl_booked"] == Decimal("1812.14")
    assert kwargs["partial_qty_booked"] == 13
    assert kwargs["is_paper"] is True


@pytest.mark.asyncio  # type: ignore[misc]
async def test_persist_without_partial_is_unchanged() -> None:
    repo = _repo()
    rec = {
        "agent_trade_id": "TRD-2",
        "net_pnl": -250.5,
        "total_costs": 20.0,
        "exit_price": 99.0,
        "exit_ts": "2026-09-24T12:07:12+05:30",
    }
    await _persist_closed_trades(repo, [rec], is_paper=False)

    args, kwargs = repo.update_pnl.call_args
    assert args[1] == Decimal("-250.5")
    assert kwargs["partial_pnl_booked"] is None
    assert kwargs["partial_qty_booked"] is None
