"""flat_resistance_breakout (hypothesis Q2): level detection, entry condition, stop, spec."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from src.strategies.indicators.library import flat_resistance
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import ConditionCompiler, USSCompiler
from src.strategies.uss.schema import Condition
from src.strategies.uss.validator import USSValidator

_SPEC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "strategies"
    / "builtin"
    / "flat_resistance_breakout.json"
)
_PEAK1, _PEAK2, _BREAK = 5 * 5 + 2, 20 * 5 + 2, 40 * 5 + 2  # mid-week bars


def _frame(breakout_close: float = 102.5, peak2: float = 100.0, n: int = 45 * 5) -> pd.DataFrame:
    """Slowly sinking base (so no base week is a swing high) with two single-day
    peaks, then a breakout day on 3x volume."""
    idx = pd.bdate_range("2024-01-01", periods=n, tz="Asia/Kolkata")  # a Monday
    c = np.linspace(80.0, 78.0, n)
    c[_PEAK1], c[_PEAK2] = 100.0, peak2
    c[_BREAK:] = breakout_close
    vol = np.full(n, 2_000_000.0)
    vol[_BREAK] = 6_000_000.0
    c = pd.Series(c, index=idx)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 1.5, "close": c, "volume": vol})


def _entry(df: pd.DataFrame) -> pd.Series:
    cond = Condition(name="t", type="PRICE", params={"reference": "flat_resistance"})
    return ConditionCompiler().compile_series(cond, df)


def test_level_is_the_higher_of_two_matching_weekly_swing_highs():
    level = flat_resistance(_frame())
    assert level.iloc[_BREAK] == 100.5
    # Not usable before the first peak is 26 weeks old.
    assert level.iloc[: (5 + 26) * 5].isna().all()


def test_level_is_point_in_time():
    df = _frame()
    full = flat_resistance(df)
    for t in (150, 170, _BREAK - 1, _BREAK, len(df) - 1):
        cut = flat_resistance(df.iloc[: t + 1]).iloc[-1]
        assert (np.isnan(cut) and np.isnan(full.iloc[t])) or cut == full.iloc[t]


def test_no_level_when_second_high_is_not_flat():
    # 8% lower second high is a descending line, not a flat level.
    assert flat_resistance(_frame(peak2=92.0)).iloc[: _BREAK + 1].isna().all()


def test_level_dies_after_a_weekly_close_above_it():
    level = flat_resistance(_frame())
    # The breakout week closed above it, so 100.5 is no longer offered.
    assert not (level.iloc[_BREAK + 5 :] == 100.5).any()


def test_entry_fires_once_on_the_fresh_close_through_the_level():
    fired = _entry(_frame())
    assert bool(fired.iloc[_BREAK]) is True
    assert int(fired.sum()) == 1


def test_entry_needs_one_percent_clearance_and_is_long_only():
    assert not _entry(_frame(breakout_close=101.0)).any()  # above 100.5, below 101.5
    cond = Condition(name="t", type="PRICE", params={"reference": "flat_resistance"})
    assert not ConditionCompiler().compile_series(cond, _frame(), short=True).any()


def test_entry_rejects_extended_breakouts():
    assert not _entry(_frame(breakout_close=130.0)).any()


def test_spec_valid_and_stop_sits_one_atr_under_the_level():
    spec = JSONParser().parse(json.loads(_SPEC.read_text()))
    res = USSValidator(max_stop_loss_pct=8.0, min_rr_ratio=1.8).validate(spec)
    assert res.ok, res.errors
    df = _frame()
    compiled = USSCompiler().compile(spec)
    sl = compiled.compute_sl(102.5, df, _BREAK)
    assert 102.5 * 0.92 <= sl < 100.5  # under the level, inside the 8% cap
    assert compiled.evaluate_candle(df, _BREAK) is not None
    assert compiled.evaluate_candle(df, _BREAK - 1) is None


def test_validator_rejects_resistance_stop_without_the_entry_condition():
    raw = json.loads(_SPEC.read_text())
    raw["entry_conditions"]["conditions"] = raw["entry_conditions"]["conditions"][1:]
    res = USSValidator(max_stop_loss_pct=8.0, min_rr_ratio=1.8).validate(JSONParser().parse(raw))
    assert not res.ok
