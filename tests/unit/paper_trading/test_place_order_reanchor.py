"""place_order re-anchors SL/target to the actual fill before arming the GTT.

Regression for the 2026-06-19 bug: signal target sat below the fill (fast open
ran past the breakout), so the GTT fired TARGET_HIT instantly at a loss.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.paper_trading.paper_engine import PaperTradingEngine


def _pool() -> MagicMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 1")
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def test_place_order_reanchors_target_above_fill() -> None:
    fill = 7615.61  # runs PAST the signal target (7608.88)
    broker = MagicMock()
    broker.get_ltp = AsyncMock(return_value={"EICHERMOT": fill})
    eng = PaperTradingEngine(broker, pool=_pool())
    eng._fill_sim._max_slippage = 0.0  # deterministic fill == ltp

    tid = await eng.place_order(
        symbol="EICHERMOT",
        direction="BUY",
        quantity=1,
        strategy_name="orb_intraday_smart",
        intended_price=7600.0,
        initial_sl=7596.45,
        initial_target=7608.88,  # below fill — the bug
        product="MIS",
    )
    assert tid is not None
    gtt = eng._gtt_sim._gtts[tid]
    # re-anchored: valid geometry around the real fill, so no instant trigger
    assert gtt.target_trigger > fill
    assert gtt.sl_trigger < fill
