"""Paper-fill slippage for option leg closes -- so a paper stop fills the way a live one will, not at the trigger.

The model is hypothesis M's frozen one (docs/hypotheses/M_condor_vs_calendar_real_prices.md, user directive
2026-10-15 "SL in paper must fire like live"): per fill, ``max(0.10 pt, 1.5% of the mark)`` against us, doubled for a
stop-loss (a market order into a moving book). Constants, not config: tuning them would let paper be made to look
good. They are judgement -- no live option fills exist yet to calibrate against -- so replace them with measured
live slippage once there are some. Spread/impact from the live book is NOT modelled here.
"""

from __future__ import annotations

_FLOOR_POINTS = 0.10
_PCT_OF_MARK = 0.015
_STOP_MULTIPLE = 2.0


def adverse_slippage(mark: float, *, stop: bool) -> float:
    """Index points a fill at ``mark`` gives up (always >= 0)."""
    base = max(_FLOOR_POINTS, _PCT_OF_MARK * max(mark, 0.0))
    return base * (_STOP_MULTIPLE if stop else 1.0)


def paper_close_price(mark: float, leg_direction: str, *, stop: bool) -> float:
    """Price a paper close of a leg fills at. Closing a SOLD leg is a buy (pays up); closing a BOUGHT leg is a
    sell (gives up). Never below zero."""
    slip = adverse_slippage(mark, stop=stop)
    return mark + slip if leg_direction == "SELL" else max(mark - slip, 0.0)
