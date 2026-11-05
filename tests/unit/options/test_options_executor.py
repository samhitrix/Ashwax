"""Tests for src/options/options_executor.py + paper place_options_structure."""

from __future__ import annotations

import pytest
from src.brokers.base import OrderRequest
from src.options.options_executor import (
    OptionsExecutionError,
    OptionsExecutor,
)
from src.options.structure_selector import OptionLeg, StructureChoice

_LOT = 50


class _RecordingBroker:
    """Records every place_order; optionally raises on the Nth call (1-based)."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.orders: list[OrderRequest] = []
        self._fail_on = fail_on_call
        self._n = 0

    async def place_order(self, order: OrderRequest) -> str:
        self._n += 1
        if self._fail_on is not None and self._n == self._fail_on:
            raise RuntimeError("broker rejected leg")
        self.orders.append(order)
        return f"OID-{self._n}"


def _long_call() -> StructureChoice:
    return StructureChoice(
        symbol="NSE:RELIANCE",
        structure_type="long_call",
        product="MARGIN",
        entry=70.0,
        stop=35.0,
        target=200.0,
        capital_required=3500,
        max_loss=3500,
        max_profit=6500,
        rr=1.86,
        expected_value=1500,
        confidence=0.6,
        reasoning="long_call",
        legs=[OptionLeg("NFO:RELIANCE24JUL2750CE", "CE", 2750.0, "BUY", 1, _LOT, 70.0, 0.5)],
    )


def _bull_call_spread() -> StructureChoice:
    return StructureChoice(
        symbol="NSE:RELIANCE",
        structure_type="bull_call_spread",
        product="MARGIN",
        entry=57.0,
        stop=28.5,
        target=200.0,
        capital_required=2850,
        max_loss=2850,
        max_profit=7150,
        rr=2.5,
        expected_value=3150,
        confidence=0.6,
        reasoning="bull_call_spread",
        legs=[
            OptionLeg("NFO:RELIANCE24JUL2750CE", "CE", 2750.0, "BUY", 1, _LOT, 70.0, 0.5),
            OptionLeg("NFO:RELIANCE24JUL2950CE", "CE", 2950.0, "SELL", 1, _LOT, 13.0, 0.2),
        ],
    )


def _protective_put() -> StructureChoice:
    return StructureChoice(
        symbol="NSE:RELIANCE",
        structure_type="protective_put",
        product="MARGIN",
        entry=2750.0,
        stop=2700.0,
        target=2950.0,
        capital_required=139000,
        max_loss=4500,
        max_profit=8000,
        rr=1.7,
        expected_value=2900,
        confidence=0.6,
        reasoning="protective_put",
        legs=[OptionLeg("NFO:RELIANCE24JUL2700PE", "PE", 2700.0, "BUY", 1, _LOT, 41.0, -0.34)],
    )


# ── live executor ────────────────────────────────────────────────────────────


async def test_long_call_places_entry_and_sl() -> None:
    broker = _RecordingBroker()
    res = await OptionsExecutor(broker).place_structure(_long_call(), "SIG1")
    # 1 entry leg + 1 SL.
    assert len(broker.orders) == 2
    entry, sl = broker.orders
    assert entry.exchange == "NFO"
    assert entry.symbol == "RELIANCE24JUL2750CE"  # prefix stripped
    assert entry.direction == "BUY"
    assert sl.order_type == "SL-M"
    assert sl.direction == "SELL"
    assert res.sl_order_id is not None
    assert len(res.leg_order_ids) == 1


async def test_spread_places_long_leg_first() -> None:
    broker = _RecordingBroker()
    res = await OptionsExecutor(broker).place_structure(_bull_call_spread(), "SIG2")
    # 2 legs + 1 SL on the long leg.
    legs = [o for o in broker.orders if o.tag == "opt"]
    assert legs[0].direction == "BUY"  # long leg placed first
    assert legs[1].direction == "SELL"
    assert len(res.leg_order_ids) == 2
    assert res.sl_order_id is not None
    # SL is on the lowest-strike long leg.
    sl = next(o for o in broker.orders if o.tag == "opt-sl")
    assert sl.symbol == "RELIANCE24JUL2750CE"


async def test_leg_failure_unwinds_filled_legs() -> None:
    # Fail on the 2nd place_order call → long leg already filled, must be squared off.
    broker = _RecordingBroker(fail_on_call=2)
    with pytest.raises(OptionsExecutionError):
        await OptionsExecutor(broker).place_structure(_bull_call_spread(), "SIG3")
    # The recorded orders include the long entry (call 1) and the unwind SELL.
    tags = [o.tag for o in broker.orders]
    assert "opt-unwind" in tags
    unwind = next(o for o in broker.orders if o.tag == "opt-unwind")
    assert unwind.direction == "SELL"  # squares off the long buy
    assert unwind.symbol == "RELIANCE24JUL2750CE"


async def test_protective_put_places_equity_leg_no_extra_sl() -> None:
    broker = _RecordingBroker()
    res = await OptionsExecutor(broker).place_structure(_protective_put(), "SIG4")
    # equity BUY (NSE) + put BUY (NFO); the put is the hedge → no separate SL order.
    eq = next(o for o in broker.orders if o.tag == "opt-pp")
    assert eq.exchange == "NSE"
    assert eq.symbol == "RELIANCE"
    assert eq.direction == "BUY"
    assert res.equity_order_id is not None
    assert res.sl_order_id is None  # protective_put is self-hedged


async def test_empty_structure_raises() -> None:
    broker = _RecordingBroker()
    empty = StructureChoice(
        symbol="NSE:X",
        structure_type="equity",
        product="DELIVERY",
        entry=1,
        stop=1,
        target=1,
        capital_required=1,
        max_loss=1,
        max_profit=1,
        rr=1,
        expected_value=0,
        confidence=0.5,
        reasoning="",
        legs=[],
    )
    with pytest.raises(OptionsExecutionError):
        await OptionsExecutor(broker).place_structure(empty, "SIG5")


# ── paper path ───────────────────────────────────────────────────────────────


class _StubMarket:
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: 70.0 for s in symbols}


async def test_paper_options_structure_opens_positions() -> None:
    from src.paper_trading.paper_engine import PaperTradingEngine

    engine = PaperTradingEngine(_StubMarket(), pool=None)
    sid = await engine.place_options_structure(_bull_call_spread(), "SIG6", "test")
    assert sid is not None
    # Both legs tracked as paper positions.
    assert len(engine.tracker.symbols()) == 2
