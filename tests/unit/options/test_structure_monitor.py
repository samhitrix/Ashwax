"""Options learning-engine PR5 — exit-side tracking for buyer-first
(structure_selector.py) options_structures rows."""

from __future__ import annotations

import asyncio

from src.options.structure_monitor import CloseDecision, evaluate_structures


def _open_row(**over: object) -> dict:
    base = dict(
        structure_id="OPT-1",
        sl_order_id="SL-1",
        target=3000.0,
        symbol="NSE:RELIANCE",
        max_loss=500.0,
    )
    base.update(over)
    return base


def test_sl_filled_detected_and_realized_pnl_is_negative_max_loss() -> None:
    rows = [_open_row()]
    decisions = evaluate_structures(rows, current_prices={}, sl_statuses={"SL-1": "FILLED"})
    assert decisions == [CloseDecision("OPT-1", "SL_FILLED", -500.0, False)]


def test_sl_pending_and_price_below_target_is_a_no_op() -> None:
    rows = [_open_row()]
    decisions = evaluate_structures(
        rows, current_prices={"NSE:RELIANCE": 2900.0}, sl_statuses={"SL-1": "PENDING"}
    )
    assert decisions == []


def test_target_reached_flags_needs_market_close_with_no_pnl_yet() -> None:
    rows = [_open_row()]
    decisions = evaluate_structures(
        rows, current_prices={"NSE:RELIANCE": 3050.0}, sl_statuses={"SL-1": "PENDING"}
    )
    assert decisions == [CloseDecision("OPT-1", "TARGET_REACHED", None, True)]


def test_sl_filled_takes_priority_over_target_reached() -> None:
    """If both conditions are somehow true in the same sweep, the SL fill is
    the one that's already real and irreversible — record that, not a target
    hit for a position the SL already closed."""
    rows = [_open_row()]
    decisions = evaluate_structures(
        rows, current_prices={"NSE:RELIANCE": 3050.0}, sl_statuses={"SL-1": "FILLED"}
    )
    assert len(decisions) == 1
    assert decisions[0].exit_reason == "SL_FILLED"


def test_protective_put_has_no_sl_order_id_only_target_check_applies() -> None:
    rows = [_open_row(sl_order_id=None)]
    decisions = evaluate_structures(rows, current_prices={"NSE:RELIANCE": 3050.0}, sl_statuses={})
    assert decisions == [CloseDecision("OPT-1", "TARGET_REACHED", None, True)]


def test_missing_price_data_is_safe_no_decision() -> None:
    rows = [_open_row()]
    decisions = evaluate_structures(rows, current_prices={}, sl_statuses={})
    assert decisions == []


def test_multiple_open_structures_evaluated_independently() -> None:
    rows = [
        _open_row(structure_id="OPT-1", sl_order_id="SL-1"),
        _open_row(structure_id="OPT-2", sl_order_id="SL-2", target=3000.0, symbol="NSE:TCS"),
    ]
    decisions = evaluate_structures(
        rows,
        current_prices={"NSE:TCS": 3100.0},
        sl_statuses={"SL-1": "PENDING", "SL-2": "PENDING"},
    )
    assert [d.structure_id for d in decisions] == ["OPT-2"]


# ── OptionsExecutor.close_structure_at_market ───────────────────────────────


class _FakeBroker:
    def __init__(self, ltp_by_symbol: dict[str, float]) -> None:
        self._ltp = ltp_by_symbol
        self.placed_orders: list[dict] = []
        self.cancelled: list[str] = []

    async def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._ltp[s] for s in symbols if s in self._ltp}

    async def place_order(self, req) -> str:
        self.placed_orders.append({"symbol": req.symbol, "direction": req.direction})
        return "ORDER-CLOSE-1"


def _leg(tradingsymbol: str, direction: str, premium: float, lots: int = 1, lot_size: int = 50):
    return {
        "tradingsymbol": tradingsymbol,
        "instrument_type": "CE",
        "strike": 2800,
        "direction": direction,
        "lots": lots,
        "lot_size": lot_size,
        "premium": premium,
    }


def test_close_structure_at_market_cancels_sl_and_computes_realized_pnl() -> None:
    from src.options.options_executor import OptionsExecutor

    broker = _FakeBroker({"NFO:RELIANCE2800CE": 120.0})
    executor = OptionsExecutor(broker)
    legs = [_leg("NFO:RELIANCE2800CE", "BUY", premium=100.0, lots=1, lot_size=50)]

    pnl = asyncio.run(executor.close_structure_at_market(legs, "SL-1", "sig-1"))

    assert broker.cancelled == ["SL-1"]
    assert len(broker.placed_orders) == 1
    assert broker.placed_orders[0]["direction"] == "SELL"  # exiting a BUY leg
    assert pnl == (120.0 - 100.0) * 50  # (exit - entry) * qty for a long leg


def test_close_structure_at_market_returns_none_when_no_ltp_available() -> None:
    from src.options.options_executor import OptionsExecutor

    broker = _FakeBroker({})  # no LTP data
    executor = OptionsExecutor(broker)
    legs = [_leg("NFO:RELIANCE2800CE", "BUY", premium=100.0)]

    pnl = asyncio.run(executor.close_structure_at_market(legs, None, "sig-1"))

    assert pnl is None


def test_close_structure_at_market_handles_multi_leg_structure() -> None:
    from src.options.options_executor import OptionsExecutor

    broker = _FakeBroker({"NFO:A": 50.0, "NFO:B": 10.0})
    executor = OptionsExecutor(broker)
    legs = [
        _leg("NFO:A", "BUY", premium=60.0, lots=1, lot_size=50),  # long leg lost value
        _leg(
            "NFO:B", "SELL", premium=15.0, lots=1, lot_size=50
        ),  # short leg gained (paid less back)
    ]

    pnl = asyncio.run(executor.close_structure_at_market(legs, None, "sig-1"))

    expected = (50.0 - 60.0) * 50 + (-1.0) * (10.0 - 15.0) * 50
    assert pnl == expected
    assert len(broker.placed_orders) == 2
