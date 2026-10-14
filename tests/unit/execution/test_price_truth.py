"""Unit tests for src/execution/price_truth.py."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import BrokerInterface, Fill, Holding, Position
from src.execution.price_truth import PriceTruthEngine, PriceTruthStatus

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 1, 15, 9, 30, 0, tzinfo=_IST)


def _fill(price: float, qty: int = 10) -> Fill:
    return Fill(
        order_id="BRK001", trade_id=f"T-{price}", quantity=qty, price=price, exchange_time=_NOW
    )


def _position(symbol: str = "INFY", avg_price: float = 1500.0, qty: int = 10) -> Position:
    return Position(
        symbol=symbol,
        exchange="NSE",
        direction="long",
        quantity=qty,
        average_price=avg_price,
        last_price=avg_price,
        pnl=0.0,
        product="INTRADAY",
    )


def _holding(
    symbol: str = "IFCI", avg_price: float = 84.27, qty: int = 145, t1_qty: int = 0
) -> Holding:
    return Holding(
        symbol=symbol,
        exchange="NSE",
        quantity=qty,
        average_price=avg_price,
        last_price=avg_price,
        pnl=0.0,
        t1_quantity=t1_qty,
    )


def _make_engine(
    fills: list[Fill] | None = None,
    positions: list[Position] | None = None,
    holdings: list[Holding] | None = None,
) -> PriceTruthEngine:
    broker = MagicMock(spec=BrokerInterface)
    broker.get_trades = AsyncMock(return_value=fills or [])
    broker.get_positions = AsyncMock(return_value=positions or [])
    broker.get_holdings = AsyncMock(return_value=holdings or [])
    return PriceTruthEngine(broker)


# ── Phase 1 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase1_ok_when_prices_match() -> None:
    engine = _make_engine(fills=[_fill(1500.0)])
    result = await engine.phase1_on_fill("BRK001", "INFY", recorded_fill_price=1500.0)
    assert result.status == PriceTruthStatus.OK
    assert result.phase == 1
    assert result.actual_price == 1500.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase1_corrected_when_prices_differ() -> None:
    engine = _make_engine(fills=[_fill(1505.0)])
    result = await engine.phase1_on_fill("BRK001", "INFY", recorded_fill_price=1500.0)
    assert result.status == PriceTruthStatus.CORRECTED
    assert result.corrected_price == 1505.0
    assert result.expected_price == 1500.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase1_no_data_when_no_fills() -> None:
    engine = _make_engine(fills=[])
    result = await engine.phase1_on_fill("BRK001", "INFY", recorded_fill_price=1500.0)
    assert result.status == PriceTruthStatus.NO_DATA


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase1_weighted_avg_multiple_fills() -> None:
    fills = [_fill(1500.0, qty=5), _fill(1510.0, qty=5)]
    engine = _make_engine(fills=fills)
    result = await engine.phase1_on_fill("BRK001", "INFY", recorded_fill_price=1505.0)
    assert result.status == PriceTruthStatus.OK
    assert result.actual_price == 1505.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase1_tolerance_within_1_paisa() -> None:
    engine = _make_engine(fills=[_fill(1500.005)])
    result = await engine.phase1_on_fill("BRK001", "INFY", recorded_fill_price=1500.0)
    assert result.status == PriceTruthStatus.OK


# ── Phase 2 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_ok_when_prices_match() -> None:
    engine = _make_engine(positions=[_position(avg_price=1500.0, qty=10)])
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.OK


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_corrected_on_price_mismatch() -> None:
    engine = _make_engine(positions=[_position(avg_price=1510.0, qty=10)])
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.CORRECTED
    assert result.corrected_price == 1510.0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_critical_on_quantity_mismatch() -> None:
    engine = _make_engine(positions=[_position(avg_price=1500.0, qty=5)])
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.CRITICAL
    assert "CRITICAL" in result.message


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_no_data_when_no_position() -> None:
    engine = _make_engine(positions=[])
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.NO_DATA


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_finds_correct_symbol() -> None:
    positions = [
        _position("RELIANCE", avg_price=2500.0, qty=5),
        _position("INFY", avg_price=1500.0, qty=10),
    ]
    engine = _make_engine(positions=positions)
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.OK


# ── Phase 2 — CNC (delivery) uses holdings, not positions ──────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_cnc_uses_holdings_average_price() -> None:
    # positions() carries a same-day sell price (98.22) that must be ignored for
    # CNC — only holdings() (84.27, the true cost basis) should be compared.
    engine = _make_engine(
        positions=[_position("IFCI", avg_price=98.22, qty=145)],
        holdings=[_holding("IFCI", avg_price=84.27, qty=145)],
    )
    result = await engine.phase2_event_gated(
        "IFCI", "BRK001", recorded_fill_price=84.27, recorded_quantity=145, product="CNC"
    )
    assert result.status == PriceTruthStatus.OK


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_cnc_corrected_uses_holdings_not_positions_price() -> None:
    engine = _make_engine(
        positions=[_position("IFCI", avg_price=98.22, qty=145)],
        holdings=[_holding("IFCI", avg_price=85.00, qty=145)],
    )
    result = await engine.phase2_event_gated(
        "IFCI", "BRK001", recorded_fill_price=84.27, recorded_quantity=145, product="CNC"
    )
    assert result.status == PriceTruthStatus.CORRECTED
    assert result.corrected_price == 85.00  # holdings price, never the 98.22 from positions()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_cnc_critical_when_holding_already_sold() -> None:
    # The exact bug found 2026-09-02: IFCI's full 145-share holding was sold
    # outside the bot (holdings quantity -> 0), but DB still recorded it open.
    # This must surface as a CRITICAL quantity mismatch, never as a price
    # "correction" toward whatever positions() happens to show that day.
    engine = _make_engine(
        positions=[_position("IFCI", avg_price=98.22, qty=145)],
        holdings=[_holding("IFCI", avg_price=84.27, qty=0)],
    )
    result = await engine.phase2_event_gated(
        "IFCI", "BRK001", recorded_fill_price=84.27, recorded_quantity=145, product="CNC"
    )
    assert result.status == PriceTruthStatus.CRITICAL
    assert "CRITICAL" in result.message


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_cnc_t1_holding_not_a_false_critical() -> None:
    """2026-09-08: CGCL, genuinely held and GTT-protected (confirmed directly
    against the broker), false-alarmed CRITICAL quantity mismatch every ~30s
    because Zerodha reports a position bought the previous trading day as
    quantity=0/t1_quantity=N until it finishes settling — real, currently-held
    shares, not a lesser state. Must resolve OK, not CRITICAL."""
    engine = _make_engine(holdings=[_holding("CGCL", avg_price=286.40, qty=0, t1_qty=45)])
    result = await engine.phase2_event_gated(
        "CGCL", "BRK001", recorded_fill_price=286.40, recorded_quantity=45, product="CNC"
    )
    assert result.status == PriceTruthStatus.OK


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_cnc_no_data_when_symbol_not_in_holdings() -> None:
    engine = _make_engine(holdings=[])
    result = await engine.phase2_event_gated(
        "IFCI", "BRK001", recorded_fill_price=84.27, recorded_quantity=145, product="CNC"
    )
    assert result.status == PriceTruthStatus.NO_DATA


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase2_mis_default_still_uses_positions() -> None:
    # product defaults to "MIS" — existing intraday behavior must be untouched.
    engine = _make_engine(
        positions=[_position("INFY", avg_price=1500.0, qty=10)],
        holdings=[_holding("INFY", avg_price=999.0, qty=10)],
    )
    result = await engine.phase2_event_gated(
        "INFY", "BRK001", recorded_fill_price=1500.0, recorded_quantity=10
    )
    assert result.status == PriceTruthStatus.OK


# ── Phase 3 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase3_ok_with_exit_fills_long() -> None:
    engine = _make_engine(fills=[_fill(1600.0)])
    result = await engine.phase3_on_exit(
        exit_order_id="EXIT001",
        symbol="INFY",
        entry_fill_price=1500.0,
        quantity=10,
        direction="BUY",
    )
    assert result.status == PriceTruthStatus.OK
    assert result.corrected_price == 1600.0
    assert "1000.00" in result.message  # P&L = (1600-1500)*10


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase3_pnl_short() -> None:
    engine = _make_engine(fills=[_fill(1400.0)])
    result = await engine.phase3_on_exit(
        exit_order_id="EXIT001",
        symbol="INFY",
        entry_fill_price=1500.0,
        quantity=10,
        direction="SELL",
    )
    assert result.status == PriceTruthStatus.OK
    assert "1000.00" in result.message  # P&L = (1500-1400)*10


@pytest.mark.asyncio  # type: ignore[misc]
async def test_phase3_no_data_when_no_fills() -> None:
    engine = _make_engine(fills=[])
    result = await engine.phase3_on_exit(
        exit_order_id="EXIT001",
        symbol="INFY",
        entry_fill_price=1500.0,
        quantity=10,
        direction="BUY",
    )
    assert result.status == PriceTruthStatus.NO_DATA
