"""Tests for the seller-desk protection-first multi-leg executor + naked-exposure
watchdog (options_spec.md §9). This is the first module in the build that can
place real (paper-simulated) orders — tests are deliberately thorough."""

from __future__ import annotations

import asyncio
import time

from src.brokers.base import Order, OrderRequest
from src.options.options_leg_executor import OptionsLegExecutor
from src.options.structure_selector import OptionLeg


def _leg(strike: float, direction: str, instrument_type: str = "CE") -> OptionLeg:
    return OptionLeg(
        tradingsymbol=f"NFO:NIFTY{int(strike)}{instrument_type}",
        instrument_type=instrument_type,
        strike=strike,
        direction=direction,
        lots=1,
        lot_size=25,
        premium=50.0,
    )


class _FakeBroker:
    """Orders FILL after `fills_after` polls; symbols in `reject_symbols` REJECT
    on first poll; everything else stays OPEN forever (times out)."""

    def __init__(
        self,
        fills_after: int = 1,
        reject_symbols: set[str] | None = None,
        ltp: dict[str, float] | None = None,
    ) -> None:
        self.placed: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self._poll_counts: dict[str, int] = {}
        self._fills_after = fills_after
        self._reject_symbols = reject_symbols or set()
        self._order_symbol: dict[str, str] = {}
        # tradingsymbol (with exchange prefix, e.g. "NFO:NIFTY23600PE") -> LTP,
        # used by close_leg()'s exit-price approximation. Missing/None entries
        # simulate an LTP fetch failure.
        self._ltp = ltp or {}

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._ltp[s] for s in symbols if s in self._ltp}

    async def place_order(self, order: OrderRequest) -> str:
        order_id = f"OID{len(self.placed)}"
        self.placed.append(order)
        self._order_symbol[order_id] = order.symbol
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    async def get_order_status(self, order_id: str) -> Order:
        sym = self._order_symbol[order_id]
        if sym in self._reject_symbols:
            status = "REJECTED"
        else:
            self._poll_counts[order_id] = self._poll_counts.get(order_id, 0) + 1
            status = "FILLED" if self._poll_counts[order_id] >= self._fills_after else "OPEN"
        return Order(
            broker_order_id=order_id,
            symbol=sym,
            direction="BUY",
            quantity=25,
            filled_quantity=25 if status == "FILLED" else 0,
            pending_quantity=0,
            status=status,
            order_type="MARKET",
            product="MARGIN",
            average_price=50.0,
            tag="",
            placed_at=None,  # type: ignore[arg-type]
        )


def _executor(broker: _FakeBroker, **kw: object) -> OptionsLegExecutor:
    return OptionsLegExecutor(
        broker, poll_interval_seconds=0.001, fill_timeout_seconds=kw.get("timeout", 0.05)
    )  # type: ignore[arg-type]


async def test_all_legs_fill() -> None:
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    result = await ex.execute_structure(
        protection_legs=[_leg(23600, "BUY", "PE")],
        income_legs=[_leg(24400, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert result.outcome == "ALL_FILLED"
    assert len(broker.placed) == 2  # protection then income, both filled


async def test_protection_leg_rejected_never_places_income() -> None:
    broker = _FakeBroker(fills_after=1, reject_symbols={"NIFTY23600PE"})
    ex = _executor(broker)
    result = await ex.execute_structure(
        protection_legs=[_leg(23600, "BUY", "PE")],
        income_legs=[_leg(24400, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert result.outcome == "ALL_REJECTED"
    assert len(broker.placed) == 1  # income leg was NEVER submitted


async def test_protection_leg_times_out_closes_any_that_filled() -> None:
    # Two protection legs: one fills fast, one never fills (times out).
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    fast_leg = _leg(23600, "BUY", "PE")
    slow_leg = _leg(23500, "BUY", "PE")

    # Make the slow leg's symbol never reach FILLED by using a huge fills_after
    # only for its polls — simplest: monkeypatch get_order_status per-symbol.
    orig = broker.get_order_status

    async def _patched(order_id: str) -> Order:
        order = await orig(order_id)
        if order.symbol == "NIFTY23500PE":
            order.status = "OPEN"  # force perpetual timeout for this leg
        return order

    broker.get_order_status = _patched  # type: ignore[method-assign]

    result = await ex.execute_structure(
        protection_legs=[fast_leg, slow_leg],
        income_legs=[_leg(24400, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert result.outcome == "PARTIAL_EMERGENCY_CLOSED"
    # fast leg's fill got market-closed; slow leg's order got cancelled; income never placed.
    close_orders = [o for o in broker.placed if o.tag == "opt-emergency-close"]
    assert len(close_orders) == 1
    assert broker.cancelled  # the never-filled leg's order was cancelled
    income_orders = [o for o in broker.placed if o.tag == "opt-income"]
    assert income_orders == []


async def test_income_zero_fills_closes_protection() -> None:
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    orig = broker.get_order_status

    async def _patched(order_id: str) -> Order:
        order = await orig(order_id)
        if order.symbol == "NIFTY24400CE":  # income leg never fills
            order.status = "OPEN"
        return order

    broker.get_order_status = _patched  # type: ignore[method-assign]

    result = await ex.execute_structure(
        protection_legs=[_leg(23600, "BUY", "PE")],
        income_legs=[_leg(24400, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert result.outcome == "PARTIAL_EMERGENCY_CLOSED"
    close_orders = [o for o in broker.placed if o.tag == "opt-emergency-close"]
    assert len(close_orders) == 1  # protection leg closed


async def test_income_partial_fill_is_degraded_not_emergency() -> None:
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    orig = broker.get_order_status

    async def _patched(order_id: str) -> Order:
        order = await orig(order_id)
        if order.symbol == "NIFTY24500CE":  # this one income leg never fills
            order.status = "OPEN"
        return order

    broker.get_order_status = _patched  # type: ignore[method-assign]

    result = await ex.execute_structure(
        protection_legs=[_leg(23600, "BUY", "PE"), _leg(24600, "BUY", "CE")],
        income_legs=[_leg(24400, "SELL", "CE"), _leg(24500, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert result.outcome == "PARTIAL_DEGRADED"
    close_orders = [o for o in broker.placed if o.tag == "opt-emergency-close"]
    assert close_orders == []  # protection NOT closed — the filled income leg is hedged


async def test_lock_for_returns_same_lock() -> None:
    ex = _executor(_FakeBroker())
    lock1 = ex.lock_for("POS1")
    lock2 = ex.lock_for("POS1")
    assert lock1 is lock2


async def test_naked_exposure_watchdog_force_closes_after_60s() -> None:
    ex = _executor(_FakeBroker())
    closed: list[str] = []

    async def _close(position_id: str) -> None:
        closed.append(position_id)

    # Already 100s stale -> fires on the very first watchdog iteration.
    ex.mark_naked("POS1", detected_at=time.monotonic() - 100.0)

    stop = asyncio.Event()
    task = asyncio.create_task(ex.naked_exposure_watchdog(_close, stop=stop, poll_seconds=0.001))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert closed == ["POS1"]
    assert ex.naked_position_ids() == []


async def test_naked_exposure_watchdog_leaves_fresh_positions_alone() -> None:
    ex = _executor(_FakeBroker())
    closed: list[str] = []

    async def _close(position_id: str) -> None:
        closed.append(position_id)

    ex.mark_naked("POS1", detected_at=time.monotonic())  # just detected, not stale

    stop = asyncio.Event()
    task = asyncio.create_task(ex.naked_exposure_watchdog(_close, stop=stop, poll_seconds=0.001))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert closed == []
    assert ex.naked_position_ids() == ["POS1"]


async def test_close_leg_returns_ltp_captured_before_the_close_order() -> None:
    leg = _leg(23600, "BUY", "PE")
    broker = _FakeBroker(ltp={leg.tradingsymbol: 42.5})
    ex = _executor(broker)
    exit_price = await ex.close_leg(leg, "SIG1")
    assert exit_price == 42.5
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.direction == "SELL"  # BUY leg closes by SELLing
    assert placed.order_type == "MARKET"


async def test_close_leg_short_leg_closes_by_buying() -> None:
    leg = _leg(24400, "SELL", "CE")
    broker = _FakeBroker(ltp={leg.tradingsymbol: 10.0})
    ex = _executor(broker)
    exit_price = await ex.close_leg(leg, "SIG1")
    assert exit_price == 10.0
    assert broker.placed[0].direction == "BUY"  # SELL leg closes by BUYing


async def test_close_leg_returns_none_when_ltp_fetch_fails() -> None:
    # tradingsymbol not in broker's ltp map -> get_ltp() returns {} for it.
    leg = _leg(23600, "BUY", "PE")
    broker = _FakeBroker(ltp={})
    ex = _executor(broker)
    exit_price = await ex.close_leg(leg, "SIG1")
    assert exit_price is None
    assert len(broker.placed) == 1  # the close order still goes through regardless


async def test_roll_leg_closes_old_and_opens_new_on_success() -> None:
    old_leg = _leg(24000, "SELL", "CE")
    new_leg = _leg(23800, "SELL", "CE")
    broker = _FakeBroker(fills_after=1, ltp={old_leg.tradingsymbol: 10.0})
    ex = _executor(broker)
    result = await ex.roll_leg(old_leg, new_leg, "OPT-1")
    assert result is new_leg
    close_orders = [o for o in broker.placed if o.tag == "opt-emergency-close"]
    open_orders = [o for o in broker.placed if o.tag == "opt-adjustment"]
    assert len(close_orders) == 1
    assert close_orders[0].symbol == "NIFTY24000CE"
    assert close_orders[0].direction == "BUY"  # SELL leg closes by BUYing
    assert len(open_orders) == 1
    assert open_orders[0].symbol == "NIFTY23800CE"
    assert open_orders[0].direction == "SELL"


async def test_roll_leg_returns_none_when_new_leg_fails_to_fill() -> None:
    old_leg = _leg(24000, "SELL", "CE")
    new_leg = _leg(23800, "SELL", "CE")
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    orig = broker.get_order_status

    async def _patched(order_id: str) -> Order:
        order = await orig(order_id)
        if order.symbol == "NIFTY23800CE":  # the new leg never fills
            order.status = "OPEN"
        return order

    broker.get_order_status = _patched  # type: ignore[method-assign]

    result = await ex.roll_leg(old_leg, new_leg, "OPT-1")
    assert result is None
    # old leg still closed even though the new one never filled (net risk-reducing)
    assert any(o.tag == "opt-emergency-close" for o in broker.placed)
    assert broker.cancelled  # the stuck new-leg order got cancelled


async def test_close_leg_returns_none_when_broker_get_ltp_raises() -> None:
    class _RaisingBroker(_FakeBroker):
        async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
            raise ConnectionError("broker unreachable")

    leg = _leg(23600, "BUY", "PE")
    broker = _RaisingBroker()
    ex = _executor(broker)
    exit_price = await ex.close_leg(leg, "SIG1")
    assert exit_price is None
    assert len(broker.placed) == 1  # close still proceeds despite the LTP failure


async def test_mis_leg_enters_and_closes_as_mis_never_nrml() -> None:
    """An NRML close against an MIS short opens a new position instead of flattening it, so
    every order for a leg must carry that leg's own product."""
    from dataclasses import replace

    leg = replace(_leg(24400, "SELL", "CE"), product="INTRADAY")
    broker = _FakeBroker(fills_after=1, ltp={leg.tradingsymbol: 10.0})
    ex = _executor(broker)
    await ex.execute_structure(protection_legs=[], income_legs=[leg], signal_id="SIG1")
    await ex.close_leg(leg, "SIG1")
    assert [o.product for o in broker.placed] == ["INTRADAY", "INTRADAY"]


async def test_legs_default_to_nrml() -> None:
    broker = _FakeBroker(fills_after=1)
    ex = _executor(broker)
    await ex.execute_structure(
        protection_legs=[_leg(23600, "BUY", "PE")],
        income_legs=[_leg(24400, "SELL", "CE")],
        signal_id="SIG1",
    )
    assert {o.product for o in broker.placed} == {"MARGIN"}


def test_product_survives_db_round_trip() -> None:
    """Restart restores legs from the stored JSON; losing the product would make the
    restored MIS leg close with an NRML order."""
    from dataclasses import replace

    from src.db.repositories.options import _serialize_leg

    leg = replace(_leg(24400, "SELL", "CE"), product="INTRADAY")
    assert OptionLeg(**_serialize_leg(leg)) == leg
