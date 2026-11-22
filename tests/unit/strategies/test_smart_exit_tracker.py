"""SmartExitTracker (live) rides the SAME exit logic as the backtest stepper.

Verifies a long ORB-style position: 50% partial + breakeven at 1R, ATR trail
ratchet past 1.5R, and EOD square-off — the Pine smart-exit, live.
"""

from __future__ import annotations

import pandas as pd
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import SmartExitTracker, USSCompiler
from src.strategies.uss.schema import Direction

_SPEC = {
    "meta": {"name": "t", "direction": "LONG", "timeframe": "5MIN"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [{"name": "e", "type": "PRICE", "condition": "price_above", "threshold": 1}],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 2.0},
    "target": {"method": "risk_reward", "ratio": 3.0},
    "exit_conditions": {
        "trailing_stop": {"method": "atr_trail", "atr_period": 14, "atr_multiplier": 1.0},
        "breakeven_at_r": 1.0,
        "partial_book_r": 1.0,
        "trail_activation_r": 1.5,
        "reversal_exit": True,
    },
    "intraday": {
        "range_window": "0915-0920",
        "entry_window": "0920-1500",
        "exit_window": "1510-1530",
        "timezone": "Asia/Kolkata",
    },
}

# entry=100, risk=2 (fixed 2%), 1R=102, 1.5R=103, target=106.
_ENTRY, _SL, _TARGET = 100.0, 98.0, 106.0


def _spec():
    return USSCompiler().compile(JSONParser().parse(_SPEC)).spec


def _df(bars: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    o, h, low, c = zip(*bars, strict=False)
    idx = pd.date_range("2026-06-15 09:15", periods=len(bars), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [1e6] * len(bars)}, index=idx
    )


def test_partial_breakeven_trail_then_eod() -> None:
    # 20 flat bars (ATR warm-up) inside the range, then a clean run-up.
    bars = [(100.0, 100.5, 99.5, 100.0)] * 20
    runup = [
        (100.0, 101.5, 100.0, 101.0),  # below 1R
        (101.0, 102.5, 101.0, 102.3),  # high >= 1R (102) → partial + breakeven
        (102.3, 103.6, 102.3, 103.5),  # peak >= 1.5R (103) → trail engages
        (103.5, 104.0, 103.5, 104.0),  # bullish, no reversal; held into exit window
    ]
    tracker = SmartExitTracker(_spec(), _ENTRY, _SL, _TARGET, Direction.LONG)
    bars += runup
    n0 = 20

    be1 = tracker.step(_df(bars[: n0 + 1]))  # below 1R
    assert not be1.exit and not be1.booked_partial

    be2 = tracker.step(_df(bars[: n0 + 2]))  # 1R bar
    assert be2.booked_partial is True
    assert tracker.partial_booked is True
    assert tracker.current_sl == _ENTRY  # stop pulled to breakeven

    be3 = tracker.step(_df(bars[: n0 + 3]))  # 1.5R bar → trail
    assert be3.sl_changed is True
    assert tracker.current_sl > _ENTRY  # trailed above entry

    be4 = tracker.step(_df(bars[: n0 + 4]), in_exit=True)  # EOD square-off
    assert be4.exit is True
    assert be4.reason == "EOD Exit"
