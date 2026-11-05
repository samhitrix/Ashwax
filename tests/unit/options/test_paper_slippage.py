"""Paper leg closes must not fill at the trigger/LTP: they pay modelled slippage (hypothesis M's frozen model)."""

from __future__ import annotations

import pytest
from src.options.paper_slippage import adverse_slippage, paper_close_price

from tests.unit.options.test_options_intraday_short_premium import (
    _AT,
    _new_desk,
    _open,
)


def test_slippage_is_the_larger_of_floor_and_percent_and_doubles_for_a_stop() -> None:
    assert adverse_slippage(4.0, stop=False) == pytest.approx(0.10)  # floor beats 1.5% x 4
    assert adverse_slippage(500.0, stop=False) == pytest.approx(7.5)  # 1.5% x 500
    assert adverse_slippage(500.0, stop=True) == pytest.approx(15.0)  # stop = x2


def test_closing_a_short_pays_up_and_closing_a_long_gives_up() -> None:
    assert paper_close_price(100.0, "SELL", stop=True) == pytest.approx(103.0)
    assert paper_close_price(100.0, "BUY", stop=True) == pytest.approx(97.0)
    assert paper_close_price(0.05, "BUY", stop=False) == 0.0  # never negative


class _HintingExecutor:
    """A paper-style executor: remembers the price it was hinted and fills there."""

    def __init__(self) -> None:
        self.hinted: dict[str, float] = {}

    def hint_prices(self, prices: dict[str, float]) -> None:
        self.hinted.update(prices)

    async def close_leg(self, leg, signal_id):  # type: ignore[no-untyped-def]
        return self.hinted[leg.tradingsymbol]


async def test_a_paper_stop_fills_past_the_trigger_and_records_the_slippage() -> None:
    desk, _ = _new_desk()
    ex = _HintingExecutor()
    desk._executor_for_position = lambda pos: ex  # type: ignore[method-assign]
    pos = _open(desk)
    leg = next(
        x for x in pos.income_legs if x.tradingsymbol == "SC"
    )  # entry 40, 23% stop -> trigger 49.2
    ok = await desk.close_single_leg(
        pos, leg, ltp=49.5, now=_AT, reason="SHORT_PREMIUM_SL 23%", trigger=49.2
    )
    assert ok
    rec = pos.closed_legs[0]
    assert rec["exit_premium"] == pytest.approx(49.5 + adverse_slippage(49.5, stop=True))
    assert rec["exit_premium"] > 49.2  # never at the trigger
    assert rec["paper_slippage"] == pytest.approx(adverse_slippage(49.5, stop=True))
    assert pos.closed_pnl == pytest.approx(-(rec["exit_premium"] - 40.0) * 65)


async def test_a_non_stop_close_pays_the_normal_not_the_doubled_slippage() -> None:
    desk, _ = _new_desk()
    ex = _HintingExecutor()
    desk._executor_for_position = lambda pos: ex  # type: ignore[method-assign]
    pos = _open(desk)
    leg = pos.income_legs[0]
    await desk.close_single_leg(pos, leg, ltp=30.0, now=_AT, reason="SHORT_PREMIUM_TIME_EXIT 15:01")
    assert pos.closed_legs[0]["paper_slippage"] == pytest.approx(adverse_slippage(30.0, stop=False))
