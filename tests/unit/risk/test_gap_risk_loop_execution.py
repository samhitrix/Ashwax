"""Regression test for _gap_risk_loop's decision execution (src/orchestrator.py).

Root cause (found 2026-08-21): on a Friday, evaluate_gap_risk() correctly
returns REDUCE (trim to suggested_quantity) for positions only over a
portfolio-level exposure cap, and CLOSE only for positions with real binary
gap risk — but the loop treated CLOSE and REDUCE identically, fully exiting
every position via order_monitor._exec_sl_gap_breach() regardless of the
decision. Result: all 11 open swing positions were fully closed on a Friday
when only 2 actually warranted a full close; the other 9 should have kept
half their position. Fixed by dispatching REDUCE -> book_partial(fraction)
and CLOSE -> force_exit(..., "GAP_RISK_CLOSE").
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import src.orchestrator as orch_mod
import src.risk.gap_risk as gap_risk_mod

_IST = ZoneInfo("Asia/Kolkata")
_FRIDAY_1430 = datetime_module.datetime(2026, 8, 21, 14, 30, 5, tzinfo=_IST)


class _FakeOrderMonitor:
    def __init__(self, positions: list[SimpleNamespace]) -> None:
        self._positions = positions
        self.book_partial_calls: list[tuple[str, float, float]] = []
        self.force_exit_calls: list[tuple[str, float, str]] = []

    def get_monitored(self) -> list[SimpleNamespace]:
        return self._positions

    async def book_partial(self, agent_trade_id: str, fraction: float, price: float) -> None:
        self.book_partial_calls.append((agent_trade_id, fraction, price))

    async def force_exit(self, agent_trade_id: str, price: float, reason: str) -> None:
        self.force_exit_calls.append((agent_trade_id, price, reason))


class _FakeBroker:
    def __init__(self, ltp_map: dict[str, float]) -> None:
        self._ltp_map = ltp_map

    async def get_positions(self) -> None:
        return None

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {s: self._ltp_map.get(s, 0.0) for s in symbols}


def _pos(symbol: str, tid: str, qty: int, fill: float, sl: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        agent_trade_id=tid,
        quantity=qty,
        fill_price=fill,
        product="CNC",
        current_sl=sl,
    )


@pytest.mark.asyncio  # type: ignore[misc]
async def test_reduce_decision_trims_instead_of_fully_closing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reduce_pos = _pos("NSE:REDUCEME", "TRD-1", qty=10, fill=100.0, sl=90.0)
    close_pos = _pos("NSE:CLOSEME", "TRD-2", qty=10, fill=100.0, sl=90.0)
    order_monitor = _FakeOrderMonitor([reduce_pos, close_pos])
    broker = _FakeBroker({"NSE:REDUCEME": 105.0, "NSE:CLOSEME": 108.0})

    def fake_evaluate_gap_risk(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                symbol="NSE:REDUCEME",
                action="REDUCE",
                reason="Portfolio overnight exposure 12.3% > 10% — trim",
                suggested_quantity=5,
                gap_risk_pct=0.0,
            ),
            SimpleNamespace(
                symbol="NSE:CLOSEME",
                action="CLOSE",
                reason="Gap risk 8.7% >= close threshold 6.0% -> EXIT_FULLY",
                suggested_quantity=0,
                gap_risk_pct=0.087,
            ),
        ]

    stop = asyncio.Event()

    async def fake_sleep(_seconds: float) -> None:
        stop.set()

    class _FixedDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime_module.datetime:  # noqa: ANN401
            return _FRIDAY_1430

    orig_sleep = asyncio.sleep
    orig_dt = datetime_module.datetime
    orig_eval = gap_risk_mod.evaluate_gap_risk
    orig_logs = orch_mod._LOGS
    try:
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        datetime_module.datetime = _FixedDatetime  # type: ignore[misc]
        gap_risk_mod.evaluate_gap_risk = fake_evaluate_gap_risk  # type: ignore[assignment]
        orch_mod._LOGS = tmp_path  # type: ignore[assignment]
        await orch_mod._gap_risk_loop(
            broker=broker,
            order_monitor=order_monitor,
            total_capital=1_000_000.0,
            holidays=set(),
            shared={},
            gap_config={},
            stop=stop,
        )
    finally:
        asyncio.sleep = orig_sleep
        datetime_module.datetime = orig_dt
        gap_risk_mod.evaluate_gap_risk = orig_eval
        orch_mod._LOGS = orig_logs

    # REDUCE: trims to suggested_quantity (5 of 10 -> fraction 0.5), position stays open.
    assert order_monitor.book_partial_calls == [("TRD-1", 0.5, 105.0)]
    assert order_monitor.force_exit_calls == [("TRD-2", 108.0, "GAP_RISK_CLOSE")]
