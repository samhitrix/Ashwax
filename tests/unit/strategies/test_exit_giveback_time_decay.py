"""Giveback lock (profit ratchet) and session-clock stop decay in the shared
exit stepper. Both are tighten-only and read MFE up to the PRIOR bar."""

from __future__ import annotations

import pandas as pd
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import SmartExitTracker, USSCompiler
from src.strategies.uss.schema import Direction


def _spec(exit_conditions: dict):
    raw = {
        "meta": {"name": "t", "direction": "BOTH", "timeframe": "5MIN"},
        "entry_conditions": {
            "logic": "AND",
            "conditions": [
                {"name": "e", "type": "PRICE", "condition": "price_above", "threshold": 1}
            ],
        },
        "stop_loss": {"method": "fixed_pct", "fixed_pct": 2.0},
        "target": {"method": "risk_reward", "ratio": 3.0},
        "exit_conditions": {"breakeven_at_r": 1.0, **exit_conditions},
        "intraday": {
            "range_window": "0915-0920",
            "entry_window": "0930-1430",
            "exit_window": "1510-1514",
            "timezone": "Asia/Kolkata",
        },
    }
    return USSCompiler().compile(JSONParser().parse(raw)).spec


def _df(bars: list[tuple[float, float, float, float]], start: str = "2026-09-22 11:00"):
    o, h, low, c = zip(*bars, strict=False)
    idx = pd.date_range(start, periods=len(bars), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {"open": o, "high": h, "low": low, "close": c, "volume": [1e6] * len(bars)}, index=idx
    )


def _run(tracker: SmartExitTracker, bars, start: str = "2026-09-22 11:00"):
    out = []
    for n in range(1, len(bars) + 1):
        be = tracker.step(_df(bars[:n], start))
        out.append(be)
        if be.exit:
            break
    return out


_GB = {"giveback_lock": {"activate_mfe_r": 0.5, "lock_fraction": 0.5}}


def test_long_giveback_locks_half_of_mfe_and_exits_green() -> None:
    # entry 100, sl 98 → 1R = 2. Run-up to 101.4 = 0.7R, then reversal.
    t = SmartExitTracker(_spec(_GB), 100.0, 98.0, 106.0, Direction.LONG)
    res = _run(
        t,
        [
            (100.0, 101.4, 99.9, 101.2),  # MFE 0.7R (lock not applied on the same bar)
            (101.2, 101.3, 100.9, 101.0),  # lock → 100 + 0.5*0.7*2 = 100.7
            (101.0, 101.0, 100.2, 100.3),  # low pierces 100.7 → exit at the lock
        ],
    )
    assert not res[0].sl_changed
    assert res[1].sl_changed and abs(t.current_sl - 100.7) < 1e-9
    assert res[2].exit and abs(res[2].exit_price - 100.7) < 1e-9
    assert res[2].exit_price > 100.0  # green trade stayed green


def test_giveback_inactive_below_threshold() -> None:
    t = SmartExitTracker(_spec(_GB), 100.0, 98.0, 106.0, Direction.LONG)
    res = _run(t, [(100.0, 100.8, 99.9, 100.6), (100.6, 100.7, 100.2, 100.4)])  # MFE 0.4R
    assert not any(r.sl_changed for r in res)
    assert t.current_sl == 98.0


def test_giveback_never_loosens() -> None:
    t = SmartExitTracker(_spec(_GB), 100.0, 98.0, 106.0, Direction.LONG)
    _run(t, [(100.0, 101.6, 99.9, 101.5), (101.5, 101.55, 101.2, 101.3)])  # lock @ 100.8
    sl_after = t.current_sl
    _run(t, [(101.3, 101.4, 101.0, 101.1)])  # lower MFE bar cannot pull the stop back
    assert t.current_sl >= sl_after


def test_short_homefirst_case_ends_green() -> None:
    # 2026-09-22 HOMEFIRST: short 1208.38, SL 1234.40, low 1190 (0.71R), closed 1212.10.
    t = SmartExitTracker(_spec(_GB), 1208.38, 1234.40, 1165.70, Direction.SHORT)
    res = _run(
        t,
        [
            (1208.0, 1209.0, 1190.0, 1195.0),
            (1195.0, 1198.0, 1193.0, 1196.0),  # lock → 1208.38 - 0.5*18.38 = 1199.19
            (1196.0, 1212.5, 1195.0, 1212.1),  # rally through the lock
        ],
    )
    assert res[-1].exit
    assert abs(res[-1].exit_price - 1199.19) < 0.01
    assert res[-1].exit_price < 1208.38  # profit on a short


def test_time_decay_tightens_stop_after_clock() -> None:
    spec = _spec({"time_decay": [{"at": "14:45", "lock_r": -0.5}]})
    t = SmartExitTracker(spec, 100.0, 98.0, 106.0, Direction.LONG)
    res = _run(t, [(100.0, 100.2, 99.6, 99.8), (99.8, 100.0, 99.5, 99.7)], start="2026-09-22 14:40")
    assert not res[0].sl_changed  # 14:40 — before the step
    assert res[1].sl_changed and abs(t.current_sl - 99.0) < 1e-9  # 14:45 → -0.5R


def test_time_decay_exits_at_close_when_price_already_through() -> None:
    spec = _spec({"time_decay": [{"at": "14:45", "lock_r": -0.5}]})
    t = SmartExitTracker(spec, 100.0, 98.0, 106.0, Direction.LONG)
    res = _run(t, [(98.8, 98.9, 98.5, 98.6)], start="2026-09-22 14:45")
    assert res[0].exit and res[0].reason == "Lock Stop"
    assert res[0].exit_price == 98.6  # honest fill at the close, not the stop


def test_time_decay_min_mfe_gate() -> None:
    spec = _spec({"time_decay": [{"at": "14:15", "min_mfe_r": 0.3, "lock_r": 0.0}]})
    flat = SmartExitTracker(spec, 100.0, 98.0, 106.0, Direction.LONG)
    _run(flat, [(100.0, 100.2, 99.8, 100.1), (100.1, 100.2, 99.9, 100.1)], "2026-09-22 14:10")
    assert flat.current_sl == 98.0  # never showed 0.3R → no breakeven

    ran = SmartExitTracker(spec, 100.0, 98.0, 106.0, Direction.LONG)
    _run(ran, [(100.0, 100.8, 99.9, 100.5), (100.5, 100.6, 100.2, 100.4)], "2026-09-22 14:10")
    assert ran.current_sl == 100.0  # showed 0.4R → breakeven from 14:15
