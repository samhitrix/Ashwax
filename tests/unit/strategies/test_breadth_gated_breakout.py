"""breadth_gated_breakout (hypothesis D): spec validity, new USS conditions, breadth gate."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import numpy as np
import pandas as pd
from src.market_data.breadth import breadth_from_closes
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import ConditionCompiler, USSCompiler
from src.strategies.uss.validator import USSValidator

_SPEC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "strategies"
    / "builtin"
    / "breadth_gated_breakout.json"
)


def _frame(closes, volume=1_000_000.0, start="2026-01-05") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes), tz="Asia/Kolkata")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": volume})


def _cond(ctype: str, **params):
    from src.strategies.uss.schema import Condition

    return Condition(name="t", type=ctype, params=params)


def _spec():
    return JSONParser().parse(json.loads(_SPEC.read_text()))


def test_spec_passes_operator_hard_limits():
    spec = _spec()
    res = USSValidator(max_stop_loss_pct=8.0, min_rr_ratio=1.8).validate(spec)
    assert res.ok, res.errors
    assert spec.market_filter.mode == "breadth"
    assert spec.market_filter.min_breadth == 0.5
    assert spec.meta.direction == "LONG"


def test_closing_high_uses_prior_closes_not_highs():
    # Highs sit 1 above closes: a close above the prior closing high but below the
    # prior HIGH must still fire (that is the tested rule).
    df = _frame([100.0] * 10 + [100.5])
    s = ConditionCompiler().compile_series(
        _cond("PRICE", reference="closing_high", lookback_days=5), df
    )
    assert bool(s.iloc[-1]) is True
    assert not s.iloc[5:-1].any()  # flat closes never exceed their own prior max


def test_closing_high_short_mirror_is_closing_low():
    df = _frame([100.0] * 10 + [99.5])
    s = ConditionCompiler().compile_series(
        _cond("PRICE", reference="closing_high", lookback_days=5), df, short=True
    )
    assert bool(s.iloc[-1]) is True


def test_indicator_above_compares_two_smas():
    up = _frame(list(np.linspace(100, 300, 260)))
    down = _frame(list(np.linspace(300, 100, 260)))
    cond = _cond(
        "INDICATOR", indicator="SMA", period=50, condition="indicator_above", other_period=200
    )
    cc = ConditionCompiler()
    assert bool(cc.compile_series(cond, up).iloc[-1]) is True
    assert bool(cc.compile_series(cond, down).iloc[-1]) is False
    assert not cc.compile_series(cond, up).iloc[:199].any()  # SMA200 warm-up -> False


def test_turnover_above_in_crores():
    # close 100 x volume 1,000,000 = Rs 10 cr/day
    df = _frame([100.0] * 25)
    cc = ConditionCompiler()
    at = cc.compile_series(
        _cond("VOLUME", condition="turnover_above", threshold_crores=10, average_days=20), df
    )
    above = cc.compile_series(
        _cond("VOLUME", condition="turnover_above", threshold_crores=11, average_days=20), df
    )
    assert bool(at.iloc[-1]) is True
    assert bool(above.iloc[-1]) is False


def _gates(breadth):
    compiled = USSCompiler().compile(_spec())
    compiled.set_breadth_context(breadth)
    df = _frame([100.0] * 5)
    return df, compiled._market_gates(df)


def test_breadth_gate_allows_at_or_above_threshold_and_blocks_below():
    df, _ = _gates(None)
    days = [d.date() for d in df.index]
    breadth = pd.Series([0.49, 0.50, 0.51, 0.2, 0.9], index=days)
    _, (long_ok, short_ok) = _gates(breadth)
    assert list(long_ok) == [False, True, True, False, True]
    assert not short_ok.any()


def test_breadth_gate_fails_closed_without_context_or_date():
    _, (long_ok, _) = _gates(None)
    assert not long_ok.any()
    _, (long_ok, _) = _gates(pd.Series(dtype=float))
    assert not long_ok.any()
    df, _ = _gates(None)
    partial = pd.Series([0.9], index=[df.index[0].date()])
    _, (long_ok, _) = _gates(partial)
    assert list(long_ok) == [True, False, False, False, False]


def test_breadth_from_closes_matches_definition():
    idx = pd.bdate_range("2025-01-01", periods=210)
    rising = np.linspace(100, 200, 210)
    falling = np.linspace(200, 100, 210)
    closes = pd.DataFrame({f"U{i}": rising for i in range(3)} | {"D0": falling}, index=idx)
    b = breadth_from_closes(closes, sma_period=200, min_symbols=4)
    assert len(b) == 11  # first full SMA window at row 199
    assert b.iloc[-1] == 0.75
    assert breadth_from_closes(closes, sma_period=200, min_symbols=5).empty


def test_breadth_from_closes_drops_unmeasurable_symbols():
    idx = pd.bdate_range("2025-01-01", periods=5)
    closes = pd.DataFrame({"A": [1, 2, 3, 4, 5], "B": [np.nan] * 5}, index=idx, dtype=float)
    b = breadth_from_closes(closes, sma_period=2, min_symbols=1)
    assert (b == 1.0).all()  # B never counts in the denominator


def test_breadth_for_skips_db_when_no_strategy_uses_it():
    import asyncio

    from src.orchestrator import _breadth_for

    plain = SimpleNamespace(spec=SimpleNamespace(market_filter=None))
    assert asyncio.run(_breadth_for([plain], pool=None)) is None
    gated = USSCompiler().compile(_spec())
    out = asyncio.run(_breadth_for([gated], pool=None))
    assert out is not None and out.empty  # no DB -> empty -> gate blocks
