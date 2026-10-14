"""Unit tests for src/supervisor/reconciler.py.

Regression coverage for a bug found 2026-09-02: Reconciler.reconcile() expected
broker.get_positions() to return a raw {"net": [...]} dict, but the actual
BrokerInterface contract returns list[Position] — so broker_map was always
empty and EVERY DB-open trade was reported as GHOST_DB_TRADE, whether or not
the broker actually held it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import BrokerInterface, Holding, Position
from src.supervisor.reconciler import Reconciler

_IST = ZoneInfo("Asia/Kolkata")


def _position(
    symbol: str = "INFY",
    qty: int = 10,
    avg: float = 1500.0,
    product: str = "MIS",
    direction: str = "long",
) -> Position:
    return Position(
        symbol=symbol,
        exchange="NSE",
        direction=direction,
        quantity=qty,
        average_price=avg,
        last_price=avg,
        pnl=0.0,
        product=product,
    )


def _holding(symbol: str = "IFCI", qty: int = 145, avg: float = 84.27, t1_qty: int = 0) -> Holding:
    return Holding(
        symbol=symbol,
        exchange="NSE",
        quantity=qty,
        average_price=avg,
        last_price=avg,
        pnl=0.0,
        t1_quantity=t1_qty,
    )


def _make_broker(
    positions: list[Position] | None = None, holdings: list[Holding] | None = None
) -> MagicMock:
    broker = MagicMock(spec=BrokerInterface)
    broker.get_positions = AsyncMock(return_value=positions or [])
    broker.get_holdings = AsyncMock(return_value=holdings or [])
    return broker


def _trade(
    qty: int,
    fill_price: float,
    agent_trade_id: str,
    product: str = "CNC",
    entry_timestamp: datetime | None = None,
) -> dict[str, object]:
    """One DB `trades` row shaped for db_open_trades — a symbol maps to a LIST
    of these, since more than one can legitimately be open for it at once (see
    Reconciler.reconcile's docstring)."""
    return {
        "qty": qty,
        "fill_price": fill_price,
        "agent_trade_id": agent_trade_id,
        "product": product,
        "entry_timestamp": entry_timestamp,
    }


@pytest.mark.asyncio  # type: ignore[misc]
async def test_matched_position_no_gap() -> None:
    broker = _make_broker(positions=[_position("INFY", qty=10, product="MIS")])
    db_open = {"INFY": [_trade(10, 1500.0, "T1", product="MIS")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean
    assert result.gaps == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_genuine_ghost_db_trade_reported() -> None:
    # Broker has neither a position nor a holding for this symbol at all.
    broker = _make_broker(positions=[], holdings=[])
    db_open = {"IFCI": [_trade(145, 84.27, "T1")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "GHOST_DB_TRADE"
    assert result.gaps[0].symbol == "IFCI"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_same_day_cnc_buy_not_flagged_as_ghost() -> None:
    """2026-09-07: CGCL, a real CNC position filled the same day, was flagged
    GHOST_DB_TRADE every reconcile cycle — a same-day delivery buy is invisible
    to both positions() (excluded there on purpose) and get_holdings() (Kite
    only settles into holdings at T+1), so there's no way to confirm it either
    way today. Must not raise a false alarm for it."""
    broker = _make_broker(positions=[], holdings=[])
    db_open = {"CGCL": [_trade(45, 286.40, "TRD-CGCL", entry_timestamp=datetime.now(_IST))]}
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean
    assert result.gaps == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_prior_day_cnc_not_flagged_until_t_plus_1_settles() -> None:
    """2026-09-22 00:03 IST: LAURUSLABS bought 14:56 the previous day was flagged
    GHOST right after midnight — holdings hadn't settled yet. Yesterday's CNC buy
    is still inside the T+1 window; must not alarm."""
    broker = _make_broker(positions=[], holdings=[])
    db_open = {
        "LAURUSLABS": [
            _trade(14, 2018.7, "T0", entry_timestamp=datetime.now(_IST) - timedelta(days=1))
        ]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean


@pytest.mark.asyncio  # type: ignore[misc]
async def test_old_cnc_still_flagged_as_ghost() -> None:
    """The settlement exemption must not swallow the original 2026-09-02 case —
    a CNC position genuinely sold outside the bot well past T+1, with no
    trace in positions() or holdings(), is still a real gap."""
    broker = _make_broker(positions=[], holdings=[])
    db_open = {
        "IFCI": [_trade(145, 84.27, "T1", entry_timestamp=datetime.now(_IST) - timedelta(days=7))]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "GHOST_DB_TRADE"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_same_day_non_cnc_still_flagged() -> None:
    """The exemption is CNC-only — an intraday (MIS) position is immediately
    visible in positions() with no settlement delay, so a same-day MIS entry
    missing from the broker is still a genuine gap."""
    broker = _make_broker(positions=[], holdings=[])
    db_open = {
        "INFY": [_trade(10, 1500.0, "T2", product="MIS", entry_timestamp=datetime.now(_IST))]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "GHOST_DB_TRADE"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_t1_holding_not_flagged_as_ghost() -> None:
    """2026-09-08: CGCL was filled the previous trading day — no longer "same
    day" so the earlier exemption doesn't apply, and get_holdings() DOES return
    it, but Zerodha reports it as quantity=0/t1_quantity=45 (bought T+1 day ago,
    settlement pending) until it moves into `quantity`. That's still real,
    currently-held stock, not a lesser state — must not be a ghost."""
    broker = _make_broker(positions=[], holdings=[_holding("CGCL", qty=0, t1_qty=45)])
    db_open = {
        "CGCL": [
            _trade(45, 286.40, "TRD-CGCL", entry_timestamp=datetime.now(_IST) - timedelta(days=1))
        ]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean
    assert result.gaps == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_partially_settled_holding_qty_matches_db_no_mismatch() -> None:
    """t1_quantity must be counted everywhere a broker_map entry's quantity is
    read, not just the ghost check — a holding split across settled (quantity)
    and pending (t1_quantity) shares must still net to the DB's total, or it
    just trades GHOST_DB_TRADE for a false QTY_MISMATCH instead."""
    broker = _make_broker(positions=[], holdings=[_holding("CGCL", qty=20, t1_qty=25)])
    db_open = {
        "CGCL": [
            _trade(45, 286.40, "TRD-CGCL", entry_timestamp=datetime.now(_IST) - timedelta(days=2))
        ]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean
    assert result.gaps == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cnc_holding_with_no_same_day_position_is_not_a_ghost() -> None:
    # A standing swing/CNC holding untouched today shows up only in
    # get_holdings(), never get_positions() — this must NOT be flagged.
    broker = _make_broker(positions=[], holdings=[_holding("JINDALSAW", qty=50, avg=550.0)])
    db_open = {"JINDALSAW": [_trade(50, 550.0, "T2")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean


@pytest.mark.asyncio  # type: ignore[misc]
async def test_fully_sold_holding_still_reported_as_ghost() -> None:
    # The exact IFCI case: holding row exists but quantity is 0 (fully sold
    # outside the bot) — must still surface as a gap, not be masked because a
    # (zero-qty) holdings row technically exists for the symbol.
    broker = _make_broker(positions=[], holdings=[_holding("IFCI", qty=0, avg=84.27)])
    db_open = {"IFCI": [_trade(145, 84.27, "T1")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "GHOST_DB_TRADE"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cnc_same_day_sell_in_positions_does_not_mask_ghost() -> None:
    # The exact IFCI bug: normalize_position() abs()'s quantity, so a same-day
    # SELL of the full 145-share holding (raw qty -145) shows up in positions()
    # as an abs-quantity-145 DELIVERY row that coincidentally equals the DB's
    # open quantity — this must NOT be read as "still held". Holdings (qty=0,
    # fully sold) is the only source of truth for CNC.
    broker = _make_broker(
        positions=[_position("IFCI", qty=145, avg=98.22, product="DELIVERY", direction="short")],
        holdings=[_holding("IFCI", qty=0, avg=84.27)],
    )
    db_open = {"IFCI": [_trade(145, 84.27, "T1")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "GHOST_DB_TRADE"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_qty_mismatch_still_detected() -> None:
    broker = _make_broker(positions=[_position("INFY", qty=5, product="MIS")])
    db_open = {"INFY": [_trade(10, 1500.0, "T1", product="MIS")]}
    result = await Reconciler(broker).reconcile(db_open)
    assert not result.clean
    assert result.gaps[0].kind == "QTY_MISMATCH"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_orphaned_broker_position_detected() -> None:
    broker = _make_broker(positions=[_position("TCS", qty=20)])
    result = await Reconciler(broker).reconcile({})
    assert not result.clean
    assert result.gaps[0].kind == "ORPHANED_BROKER_POSITION"
    assert result.gaps[0].symbol == "TCS"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_same_symbol_two_different_products_both_reconcile_independently() -> None:
    """2026-09-08: the reconciler used to key everything by bare symbol —
    a symbol with both an open CNC swing hold AND an open MIS intraday trade
    at once silently dropped one of the two from db_open_trades, and could
    match the wrong one against the broker. A CNC holding and an MIS position
    for the same symbol are two independent broker-side positions and must
    both reconcile cleanly, completely independent of each other."""
    broker = _make_broker(
        positions=[_position("CGCL", qty=10, product="MIS")],
        holdings=[_holding("CGCL", qty=45, avg=286.40)],
    )
    db_open = {
        "CGCL": [
            _trade(45, 286.40, "TRD-SWING", product="CNC"),
            _trade(10, 275.0, "TRD-INTRADAY", product="MIS"),
        ]
    }
    result = await Reconciler(broker).reconcile(db_open)
    assert result.clean
    assert result.gaps == []
