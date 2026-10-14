"""Fidelity: the orb_intraday_smart USS spec reproduces the ORB.md Pine SMART EXIT math.

The strategy itself was deleted 2026-09-11 (PF 0.41 over 325 paper trades), but
its spec is kept inline here because it is still the only fidelity check of the
compiler's entry/target/partial/EOD smart-exit stepper, which news_catalyst_momentum
and llm_intraday_agent rely on.

Drives the REAL compiled strategy (from the inline spec below) over a hand-built
5-minute intraday day and asserts the entry/target/exit semantics match the Pine
script: entry on the breakout-bar CLOSE, target = entry + 2.5×risk, partial-book at
1R, and EOD square-off.

NOTE: the SL method deliberately deviates from Pine's "Tight ORB" (entry −
0.5×ORB_range) default — it uses ENTRY_CANDLE (the breakout candle's own low/high)
instead, so risk is not a fixed fraction of the opening range here (see
2026-07-30: entry-relative SL formulas drift with fill slippage and clipped a
winning APOLLOHOSP trade by a hair). Risk below is derived from the breakout
candle's low, not from range_fraction.

NOTE: this verifies the compiler/backtest layer. Live exit management (partial / BE /
ATR-trail / reversal) is a SEPARATE code path (OrderMonitor) and is NOT covered here.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import USSCompiler

# Verbatim copy of the deleted strategies/builtin/orb_intraday_smart.json (minus
# its _options notes) — see module docstring for why it is kept.
_SPEC: dict[str, Any] = {
    "meta": {
        "name": "orb_intraday_smart",
        "display_name": "ORB Intraday Smart Exit",
        "version": "1.0",
        "product": "MIS",
        "direction": "BOTH",
        "timeframe": "5MIN",
    },
    "universe_filter": {
        "min_price": 100,
        "min_adv_crores": 70,
        "exchanges": ["NSE"],
        "index_filter": None,
    },
    "market_filter": {"symbol": "NIFTY 50", "enabled": True, "mode": "daily_direction"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "orb_breakout",
                "type": "PRICE",
                "condition": "price_above",
                "reference": "opening_range_high",
            },
            {
                "name": "trend_filter_ema20",
                "type": "INDICATOR",
                "indicator": "EMA",
                "period": 20,
                "condition": "price_above",
            },
        ],
    },
    "stop_loss": {"method": "entry_candle", "min_sl_pct": 0.35},
    "target": {"method": "risk_reward", "ratio": 2.5},
    "exit_conditions": {
        "trailing_stop": {"method": "atr_trail", "atr_period": 14, "atr_multiplier": 1.0},
        "breakeven_at_r": 1.0,
        "partial_book_r": 1.0,
        "trail_activation_r": 1.0,
        "reversal_exit": True,
        "time_stop_days": 0,
    },
    "intraday": {
        "range_window": "0915-0920",
        "entry_window": "0920-1500",
        "exit_window": "1510-1516",
        "timezone": "Asia/Kolkata",
    },
}

# ORB window bars set the opening range: high=105, low=100 → range=5.
_ORB_HIGH, _ORB_LOW = 105.0, 100.0
# Breakout bar: open=103, high=106, low=103, close=106 → entry = close = 106;
# ENTRY_CANDLE SL = this candle's own low (103), not a fraction of the ORB range.
_ENTRY = 106.0
_BREAKOUT_LOW = 103.0
_SL = _BREAKOUT_LOW  # 103.0
_RISK = _ENTRY - _SL  # 3.0
_TARGET = _ENTRY + 2.5 * _RISK  # 113.5
_ONE_R = _ENTRY + _RISK  # 109.0
_BREAKOUT_IDX = 21


def _compiled():
    spec = JSONParser().parse(_SPEC)
    return USSCompiler().compile(spec)


def _frame(scenario: dict[int, tuple[float, float, float, float]], n: int) -> pd.DataFrame:
    """Build an IST 5-min day. Bars 0-1 are the ORB; the rest default to a flat
    consolidation inside the range (no breakout) unless overridden in `scenario`.
    Each scenario value is (open, high, low, close)."""
    idx = pd.date_range("2026-06-15 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    rows: list[tuple[float, float, float, float]] = []
    for i in range(n):
        if i in scenario:
            rows.append(scenario[i])
        elif i <= 1:  # ORB window (09:15, 09:20)
            rows.append((100.0, _ORB_HIGH, _ORB_LOW, 102.0))
        else:  # flat consolidation inside the range — no breakout
            rows.append((102.0, 102.5, 101.5, 102.0))
    o, h, low, c = zip(*rows, strict=False)
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [1_000_000.0] * n},
        index=idx,
    )


def test_entry_sl_target_match_pine() -> None:
    """Breakout then a drop back to the breakout candle's own low. Verifies
    entry = breakout close, SL = breakout-candle low (ENTRY_CANDLE), target =
    entry + 2.5×risk, no partial below 1R."""
    df = _frame(
        {
            _BREAKOUT_IDX: (103.0, 106.0, 103.0, _ENTRY),  # breakout: close > ORB high
            _BREAKOUT_IDX + 1: (106.0, 106.0, 103.0, 103.2),  # low touches candle-low SL 103.0
        },
        n=30,
    )
    sigs = _compiled().generate_backtest_signals(df)

    assert len(sigs) == 1  # exactly one trade/day (Pine orb_traded)
    s = sigs[0]
    assert s.entry_price == _ENTRY
    assert abs(s.sl - _SL) < 1e-6  # ENTRY_CANDLE = breakout candle's own low
    assert abs(s.target - _TARGET) < 1e-6  # risk_reward 2.5
    assert s.partial_r is None  # 1R never reached → no partial leg
    assert s.exit_reason == "SL Hit"


def test_partial_books_at_1r_then_eod_squareoff() -> None:
    """Breakout that runs just past 1R (books 50% + moves to breakeven) then stays
    flat into the exit window. Verifies partial_book_r is honored and EOD square-off
    fires inside 15:10-15:30."""
    scenario = {
        _BREAKOUT_IDX: (103.0, 106.0, 103.0, _ENTRY),
        _BREAKOUT_IDX + 1: (106.0, _ONE_R + 0.2, 107.0, 108.6),  # high >= 1R → partial + BE
    }
    # Bars after the 1R poke stay flat above breakeven, below 1.5R and target.
    for i in range(_BREAKOUT_IDX + 2, 75):
        scenario[i] = (108.0, 108.4, 107.6, 108.0)
    df = _frame(scenario, n=75)  # 09:15 → 15:25 (exit window covered)

    sigs = _compiled().generate_backtest_signals(df)

    assert len(sigs) == 1
    s = sigs[0]
    assert s.entry_price == _ENTRY
    assert abs(s.target - _TARGET) < 1e-6
    assert s.partial_r is not None  # 50% booked at 1R (partial_book_r honored)
    assert len(s.leg_pnls_r) == 2  # two legs: locked half + remainder
    assert s.exit_reason == "EOD Exit"
