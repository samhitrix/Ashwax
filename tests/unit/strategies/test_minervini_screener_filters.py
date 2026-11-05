"""minervini_vcp v1.2 screener filters: PLUS_DI, WEEKLY_MAX_LOW, market-cap universe cap."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from src.orchestrator import _apply_market_cap_limit
from src.strategies.indicators.library import compute, plus_di, weekly_max_low
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.validator import USSValidator

_SPEC = (
    pathlib.Path(__file__).resolve().parents[3] / "strategies" / "builtin" / "minervini_vcp.json"
)


def _frame(closes: list[float], start: str = "2026-01-05") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1_000_000.0})


def test_plus_di_high_in_uptrend_low_when_flat():
    up = _frame(list(np.linspace(100, 200, 80)))
    flat = _frame([100.0] * 80)
    assert plus_di(up, 14).iloc[-1] > 25
    assert plus_di(flat, 14).iloc[-1] == 0


def test_weekly_max_low_is_highest_completed_weekly_low():
    # 2026-01-05 is a Monday; 5 bars per week, week n trades at 100+10n, low = close-1.
    closes = [100 + 10 * (i // 5) for i in range(5 * 6)]
    df = _frame(closes)
    out = weekly_max_low(df, period=4)
    # Last row is in week 5 (close 150, low 149). Prior 3 completed weeks: lows 139/129/119.
    assert out.iloc[-1] == 149  # current partial week's running low beats the prior weeks
    # First bar of week 4 (row 20): prior 3 weeks' max low = 129 (weeks 1..3 -> 109,119,129)
    assert out.iloc[20] == max(129, df["low"].iloc[20])
    assert out.iloc[:15].isna().all()  # warm-up: fewer than period-1 completed weeks


def test_weekly_max_low_has_no_lookahead():
    rng = np.random.default_rng(7)
    df = _frame(list(100 + np.cumsum(rng.normal(0, 1, 120))))
    full = weekly_max_low(df, 10)
    for cut in (37, 63, 101):  # includes mid-week cut points
        partial = weekly_max_low(df.iloc[:cut], 10)
        pd.testing.assert_series_equal(partial, full.iloc[:cut])


def test_weekly_max_low_handles_tz_aware_index():
    df = _frame(list(np.linspace(100, 150, 60)))
    df.index = df.index.tz_localize("Asia/Kolkata")
    assert weekly_max_low(df, 10).iloc[-1] > 0


def test_minervini_v12_spec_parses_validates_and_evaluates():
    spec = JSONParser().parse(json.loads(_SPEC.read_text()))
    assert USSValidator().validate(spec).ok
    assert spec.universe_filter.max_market_cap_crores == 30000
    assert spec.universe_filter.max_price == 3000
    names = {c.name for c in spec.entry_conditions.conditions}
    assert {
        "weekly_low_floor",
        "min_price",
        "max_price",
        "min_volume",
        "plus_di",
        "rsi_momentum",
    } <= names

    # Each new condition compiles to a boolean series on a strong uptrend.
    from src.strategies.uss.compiler import ConditionCompiler

    # Noise so RSI has some down days (a pure line gives avg_loss=0 -> NaN in rsi()).
    noise = np.random.default_rng(3).normal(0, 1.5, 400)
    df = _frame(list(np.linspace(100, 300, 400) + noise))
    comp = ConditionCompiler()
    new = [
        c
        for c in spec.entry_conditions.conditions
        if c.name
        in {"weekly_low_floor", "min_price", "max_price", "min_volume", "plus_di", "rsi_momentum"}
    ]
    for cond in new:
        s = comp.compile_series(cond, df)
        assert s.dtype == bool
        assert bool(s.iloc[-1]), cond.name
    # price band rejects a stock above the ceiling
    hi = df * 20
    max_price = next(c for c in new if c.name == "max_price")
    assert not comp.compile_series(max_price, hi).iloc[-1]


def test_compute_dispatch_knows_new_indicators():
    df = _frame(list(np.linspace(100, 200, 80)))
    assert len(compute(df, "PLUS_DI", period=14)) == len(df)
    assert len(compute(df, "WEEKLY_MAX_LOW", period=52)) == len(df)


def test_market_cap_limit_drops_large_keeps_unknown():
    caps = {"SMALL": 20_000 * 1e7, "EDGE": 30_000 * 1e7, "BIG": 500_000 * 1e7}
    kept, unknown = _apply_market_cap_limit(["SMALL", "EDGE", "BIG", "NODATA"], caps, 30_000)
    assert kept == ["SMALL", "EDGE", "NODATA"]
    assert unknown == 1


# ---------------------------------------------------------------------------
# Diagnostics: per-condition explain, funnel, and Alerts-tab wiring
# ---------------------------------------------------------------------------


def test_explain_entry_reports_found_value_and_pass_fail():
    from src.strategies.uss.compiler import CompiledStrategy

    spec = JSONParser().parse(json.loads(_SPEC.read_text()))
    noise = np.random.default_rng(3).normal(0, 1.5, 400)
    df = _frame(list(np.linspace(100, 300, 400) + noise))
    rows = {r["name"]: r for r in CompiledStrategy(spec).explain_entry(df, -1)}
    assert rows["min_volume"]["value"] == 1_000_000.0 and rows["min_volume"]["passed"]
    assert rows["min_price"]["value"] == df["close"].iloc[-1]
    assert rows["max_price"]["passed"]
    assert isinstance(rows["rsi_momentum"]["value"], float) and rows["rsi_momentum"]["passed"]
    assert rows["plus_di"]["rule"]["threshold"] == 25
    # a price above the ceiling is reported as a FAIL with the value that failed it
    hi = df * 20
    hrow = {r["name"]: r for r in CompiledStrategy(spec).explain_entry(hi, -1)}["max_price"]
    assert not hrow["passed"] and hrow["value"] > 3000


def test_explain_entry_surfaces_indicator_errors_instead_of_hiding_them():
    from src.strategies.uss.compiler import CompiledStrategy

    spec = JSONParser().parse(json.loads(_SPEC.read_text()))
    df = _frame(list(np.linspace(100, 300, 400)))
    cs = CompiledStrategy(spec)
    cs._ensure_compiled(df)
    df2 = df.reset_index(drop=True)  # not a DatetimeIndex: weekly_max_low can't bucket weeks
    # compile succeeded above; value measurement on a broken frame must report ERR, not vanish
    cs._df_ref = df2
    rows = cs.explain_entry(df2, -1)
    wk = next(r for r in rows if r["name"] == "weekly_low_floor")
    assert isinstance(wk["value"], str) and wk["value"].startswith("ERR")


def test_funnel_counts_alone_and_cumulative():
    from src.orchestrator import _funnel_record, _new_scan_funnel

    f = _new_scan_funnel()
    ok = lambda n, p: {"name": n, "passed": p, "value": None, "rule": {}}  # noqa: E731
    _funnel_record(f, [ok("a", True), ok("b", True)])
    _funnel_record(f, [ok("a", False), ok("b", True)])
    _funnel_record(f, [ok("a", True), ok("b", False)])
    assert f["alone"] == {"a": 2, "b": 2}
    assert f["cumulative"] == {"a": 2, "b": 1}
    assert f["all_pass"] == 1


def test_scan_alert_raise_dedupe_replace_resolve():
    from src.infrastructure.alert_system import AlertManager
    from src.orchestrator import _sync_scan_alert

    mgr = AlertManager()
    shared = {"alert_manager": mgr}
    _sync_scan_alert(shared, "s:x", ("CRITICAL", "boom 1"))
    _sync_scan_alert(shared, "s:x", ("CRITICAL", "boom 1"))  # same condition: no duplicate
    assert [a.message for a in mgr.active()] == ["boom 1"]
    _sync_scan_alert(shared, "s:x", ("CRITICAL", "boom 2"))  # changed: replaced, not stacked
    assert [a.message for a in mgr.active()] == ["boom 2"]
    _sync_scan_alert(shared, "s:x", None)  # condition cleared: gone from the Alerts tab
    assert mgr.active() == []


def test_scan_alert_without_manager_logs_instead_of_hiding(caplog):
    import logging

    from src.orchestrator import _sync_scan_alert

    with caplog.at_level(logging.ERROR):
        _sync_scan_alert({}, "s:x", ("CRITICAL", "boom"))
    assert any("scan_alert_unavailable" in r.getMessage() for r in caplog.records)
