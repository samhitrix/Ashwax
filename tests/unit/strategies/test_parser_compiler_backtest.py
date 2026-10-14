"""Tests for JSON parser, USS compiler, and backtest engine."""

import numpy as np
import pandas as pd
import pytest
from src.strategies.backtest.engine import BacktestEngine
from src.strategies.parsers.json_parser import JSONParseError, JSONParser
from src.strategies.uss.compiler import USSCompiler
from src.strategies.uss.schema import SLMethod, TargetMethod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VCP_JSON = {
    "meta": {
        "name": "tight_consolidation_breakout",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "universe_filter": {"min_price": 100, "min_adv_crores": 25, "exchanges": ["NSE"]},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "consolidation_check",
                "type": "PATTERN",
                "pattern": "tight_range",
                "max_range_pct": 8.0,
                "lookback_days": 21,
            },
            {
                "name": "breakout_check",
                "type": "PRICE",
                "condition": "price_above",
                "reference": "consolidation_high",
                "lookback_days": 21,
            },
            {
                "name": "volume_confirmation",
                "type": "VOLUME",
                "condition": "volume_ratio_above",
                "ratio": 1.5,
                "average_days": 20,
            },
            {
                "name": "trend_filter",
                "type": "INDICATOR",
                "indicator": "SMA",
                "period": 200,
                "condition": "price_above",
            },
        ],
    },
    "stop_loss": {
        "method": "pattern_level",
        "lookback_days": 21,
        "buffer_pct": 0.5,
        "max_sl_pct": 10.0,
    },
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {
        "trailing_stop": {
            "method": "indicator_cross",
            "indicator": "EMA",
            "period": 10,
            "cross": "price_below_on_close",
        },
        "time_stop_days": 20,
    },
}


def _make_trending_df(n: int = 500, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 1000.0
    closes = [base]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.normal(0.0015, 0.012)))
    arr = np.array(closes)
    highs = arr * (1 + rng.uniform(0.001, 0.015, n))
    lows = arr * (1 - rng.uniform(0.001, 0.015, n))
    opens = lows + (highs - lows) * rng.uniform(0, 1, n)
    volumes = rng.integers(500_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": arr, "volume": volumes},
        index=idx,
    )


# ---------------------------------------------------------------------------
# JSON Parser
# ---------------------------------------------------------------------------


def test_json_parser_parses_vcp():
    spec = JSONParser().parse(_VCP_JSON)
    assert spec.meta.name == "tight_consolidation_breakout"
    assert len(spec.entry_conditions.conditions) == 4
    assert spec.stop_loss.method == SLMethod.PATTERN_LEVEL
    assert spec.target.method == TargetMethod.RISK_REWARD
    assert spec.target.ratio == 2.0
    assert spec.exit_conditions.time_stop_days == 20


def test_json_parser_parses_json_string():
    import json

    spec = JSONParser().parse(json.dumps(_VCP_JSON))
    assert spec.meta.name == "tight_consolidation_breakout"


def test_condition_enabled_defaults_true_and_round_trips():
    # Absent → enabled (preserves behavior for specs authored before the toggle).
    conds = {c.name: c for c in JSONParser().parse(_VCP_JSON).entry_conditions.conditions}
    assert all(c.enabled for c in conds.values())

    # Explicit enabled=false round-trips and does not leak into params.
    off = {
        **_VCP_JSON,
        "entry_conditions": {
            "logic": "AND",
            "conditions": [
                {**c, "enabled": False} if c["name"] == "volume_confirmation" else c
                for c in _VCP_JSON["entry_conditions"]["conditions"]
            ],
        },
    }
    parsed = {c.name: c for c in JSONParser().parse(off).entry_conditions.conditions}
    assert parsed["volume_confirmation"].enabled is False
    assert "enabled" not in parsed["volume_confirmation"].params


def test_disabled_condition_is_skipped_by_compiler():
    # A ratio-100x volume filter can never fire on synthetic volume, so when active
    # (AND logic) it blocks every entry. Disabling it must let entries through again.
    def _spec(enabled: bool) -> dict:
        return {
            **_VCP_JSON,
            "entry_conditions": {
                "logic": "AND",
                "conditions": [
                    {**c, "ratio": 100.0, "enabled": enabled}
                    if c["name"] == "volume_confirmation"
                    else c
                    for c in _VCP_JSON["entry_conditions"]["conditions"]
                ],
            },
        }

    df = _make_trending_df(500)
    on = BacktestEngine().run(USSCompiler().compile(JSONParser().parse(_spec(True))), df)
    off = BacktestEngine().run(USSCompiler().compile(JSONParser().parse(_spec(False))), df)
    assert on.total_trades == 0
    assert off.total_trades > 0


def test_json_parser_loads_symbol_whitelist():
    """universe_filter.symbols must round-trip from the JSON spec.

    Regression: _parse_universe previously dropped the `symbols` field, so a
    symbol-whitelisted strategy (e.g. orb_intraday_top) traded the whole
    universe because the orchestrator's `if _wl and sym not in _wl` guard saw
    an empty whitelist.
    """
    # Absent → None (no restriction), preserving behavior for unfiltered specs.
    assert JSONParser().parse(_VCP_JSON).universe_filter.symbols is None

    # Present → loaded verbatim.
    spec = JSONParser().parse({**_VCP_JSON, "universe_filter": {"symbols": ["ITC", "LT", "WIPRO"]}})
    assert spec.universe_filter.symbols == ["ITC", "LT", "WIPRO"]


def test_json_parser_raises_missing_meta_name():
    bad = {**_VCP_JSON, "meta": {"product": "DELIVERY"}}
    with pytest.raises(JSONParseError, match="meta.name"):
        JSONParser().parse(bad)


def test_json_parser_raises_missing_entry_conditions():
    bad = {k: v for k, v in _VCP_JSON.items() if k != "entry_conditions"}
    with pytest.raises(JSONParseError, match="Missing required field"):
        JSONParser().parse(bad)


def test_json_parser_raises_empty_conditions():
    bad = {**_VCP_JSON, "entry_conditions": {"logic": "AND", "conditions": []}}
    with pytest.raises(JSONParseError, match="must not be empty"):
        JSONParser().parse(bad)


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def test_compiler_produces_compiled_strategy():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    assert compiled.spec.meta.name == "tight_consolidation_breakout"


def test_evaluate_candle_returns_signal_or_none():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(300)
    result = compiled.evaluate_candle(df, 299)
    assert result is None or hasattr(result, "entry_price")


def test_compute_sl_below_entry():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(100)
    entry = float(df["close"].iloc[-1])
    sl = compiled.compute_sl(entry, df, 99)
    assert sl < entry


def test_compute_target_above_entry():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(50)
    entry = float(df["close"].iloc[-1])
    sl = entry * 0.95
    target = compiled.compute_target(entry, sl)
    assert target > entry


_RS_ONLY_JSON = {
    "meta": {"name": "rs_only", "product": "DELIVERY", "direction": "LONG", "timeframe": "DAILY"},
    "universe_filter": {"min_price": 100, "min_adv_crores": 25, "exchanges": ["NSE"]},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "rs_rating",
                "type": "INDICATOR",
                "indicator": "RS_PERCENTILE",
                "condition": "value_above",
                "threshold": 70,
                "period": 252,
            }
        ],
    },
    "stop_loss": {
        "method": "pattern_level",
        "lookback_days": 21,
        "buffer_pct": 0.5,
        "max_sl_pct": 10.0,
    },
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {"time_stop_days": 20},
}


def _make_post_base_breakout_df(n: int = 300) -> pd.DataFrame:
    """Prior uptrend to a spike, a base/pullback below it, then a modest
    breakout still short of the spike — the shape of a real VCP breakout day,
    where price is NOT yet near its own trailing-year high."""
    closes = np.concatenate(
        [np.linspace(100, 200, 100), np.linspace(200, 150, 100), np.linspace(150, 170, 100)]
    )[:n]
    idx = pd.date_range("2021-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "open": closes * 0.995,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000.0),
        },
        index=idx,
    )


def test_rs_percentile_self_referential_lags_a_fresh_breakout():
    """Root cause of the "VCP entries taken late" bug: with no override,
    RS_PERCENTILE ranks a symbol against its OWN trailing history, which
    requires it to already be near its own 1yr high — not yet true on the
    breakout day itself, right after a base."""
    spec = JSONParser().parse(_RS_ONLY_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_post_base_breakout_df()
    assert compiled.evaluate_candle(df, len(df) - 1) is None


def test_rs_percentile_override_fires_on_the_breakout_bar():
    """A cross-sectional RS percentile (this symbol ranked vs its peer
    universe, e.g. from RSRankingEngine) should let the same bar pass even
    though the self-referential calc alone would not — the fix for late
    swing/VCP entries."""
    spec = JSONParser().parse(_RS_ONLY_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_post_base_breakout_df()
    compiled.set_rs_percentile_override(85.0)
    result = compiled.evaluate_candle(df, len(df) - 1)
    assert result is not None
    assert result.entry_price == pytest.approx(float(df["close"].iloc[-1]))


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------


def test_backtest_fails_insufficient_trades():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(50)
    result = BacktestEngine().run(compiled, df)
    assert not result.passed
    assert "insufficient" in result.reject_reason


def test_backtest_returns_result_on_sufficient_data():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(500)
    result = BacktestEngine().run(compiled, df)
    assert isinstance(result.passed, bool)
    assert isinstance(result.win_rate, float)
    assert isinstance(result.profit_factor, float)


def test_backtest_summary_contains_name():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(300)
    result = BacktestEngine().run(compiled, df)
    assert "tight_consolidation_breakout" in result.summary()
    assert "trades=" in result.summary()


def test_backtest_max_drawdown_non_negative():
    spec = JSONParser().parse(_VCP_JSON)
    compiled = USSCompiler().compile(spec)
    df = _make_trending_df(500)
    result = BacktestEngine().run(compiled, df)
    assert result.max_drawdown_r >= 0.0


# ---------------------------------------------------------------------------
# Trailing-stop regression — repro of the 0% win-rate bug
# ---------------------------------------------------------------------------

_TRAIL_REPRO_SPEC = {
    "meta": {
        "name": "trail_repro",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PRICE", "condition": "price_above", "threshold": 99.5}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {
        "trailing_stop": {"method": "indicator_cross", "indicator": "EMA", "period": 10}
    },
}


def _trail_repro_df() -> pd.DataFrame:
    # Bars 0-14 flat at 99 (no entry). Bar 15 breaks out (close 100, entry).
    # Bar 16 is a normal pullback: low dips to 98 — above the 95 structural
    # stop but at the lagging EMA. Then price marches to 111; target=110 (+2R).
    closes = [99.0] * 15 + [
        100.0,
        99.6,
        101.0,
        102.5,
        104.0,
        105.5,
        107.0,
        108.5,
        110.2,
        111.0,
        111.0,
    ]
    highs = [99.0] * 15 + [
        100.0,
        100.0,
        101.3,
        102.8,
        104.3,
        105.8,
        107.3,
        108.8,
        110.5,
        111.3,
        111.3,
    ]
    lows = [99.0] * 15 + [
        100.0,
        98.0,
        100.8,
        102.2,
        103.7,
        105.2,
        106.7,
        108.2,
        109.9,
        110.7,
        110.7,
    ]
    n = len(closes)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000.0] * n},
        index=idx,
    )


def test_trailing_stop_does_not_kill_winner_before_profit():
    """An indicator_cross trail must not stop a clear winner near entry on a
    routine pullback before the trade is in profit. The trade runs 100->111
    with target=110 (+2R); the old EMA*0.999 trail exited at a small loss on
    bar 16, producing the 0% win-rate seen in backtest.log."""
    spec = JSONParser().parse(_TRAIL_REPRO_SPEC)
    compiled = USSCompiler().compile(spec)
    signals = compiled.generate_backtest_signals(_trail_repro_df())

    assert len(signals) >= 1
    assert signals[0].pnl_r is not None
    assert signals[0].pnl_r > 1.5  # target win (+2R), not a near-entry loss


# ---------------------------------------------------------------------------
# VCP fidelity gaps — declining-volume, close-based MA exit, RS gate
# ---------------------------------------------------------------------------


def _converging_df(volume: list[float]) -> pd.DataFrame:
    """80 bars whose high-low range contracts (60>30>15 window range%).
    `volume` controls whether volume also dries up across the contractions."""
    n = 80
    amp = np.linspace(15.0, 1.0, n)  # shrinking volatility envelope
    mid = 100.0
    highs = mid + amp
    lows = mid - amp
    closes = np.full(n, mid)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": volume},
        index=idx,
    )


_VCP_PATTERN_SPEC = {
    "meta": {"name": "vcp_only", "product": "DELIVERY", "direction": "LONG", "timeframe": "DAILY"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "vcp",
                "type": "PATTERN",
                "pattern": "vcp_contraction",
                "period1": 60,
                "period2": 30,
                "period3": 15,
            }
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {"time_stop_days": 20},
}


def test_vcp_contraction_requires_declining_volume():
    """VCP must match only when volume dries up across the contractions.
    Same contracting price ranges, opposite volume trends."""
    n = 80
    compiled = USSCompiler().compile(JSONParser().parse(_VCP_PATTERN_SPEC))

    falling_vol = list(np.linspace(5_000_000.0, 1_000_000.0, n))
    rising_vol = list(np.linspace(1_000_000.0, 5_000_000.0, n))

    mask_falling = compiled._entry_mask(_converging_df(falling_vol))
    mask_rising = compiled._entry_mask(_converging_df(rising_vol))

    assert bool(mask_falling.iloc[-1]) is True  # ranges + volume contracting
    assert bool(mask_rising.iloc[-1]) is False  # volume expanding → not VCP


_BREAKOUT_OR_PULLBACK_SPEC = {
    "meta": {
        "name": "breakout_or_pullback_only",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {
                "name": "entry",
                "type": "PATTERN",
                "pattern": "breakout_or_pullback",
                "pivot_lookback_days": 15,
                "post_breakout_days": 10,
                "tight_days": 3,
                "max_daily_range_pct": 3.5,
                "volume_avg_days": 20,
                "volume_dryup_ratio": 0.8,
            }
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {"time_stop_days": 20},
}


def _flat_baseline_df(
    n: int,
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    opens = [100.0] * n
    volumes = [1_000_000.0] * n
    return opens, highs, lows, closes, volumes


def test_breakout_or_pullback_fires_on_raw_breakout_alone():
    """A fresh breakout with no prior pause must still fire (the raw-breakout
    leg), same as the old breakout_above_pivot condition did."""
    opens, highs, lows, closes, volumes = _flat_baseline_df(20)
    opens.append(100.0)
    highs.append(116.0)
    lows.append(99.0)
    closes.append(115.0)  # fresh high, clears the trailing 15-day max (~101)
    volumes.append(2_000_000.0)
    idx = pd.date_range("2021-01-01", periods=21, freq="B")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )

    compiled = USSCompiler().compile(JSONParser().parse(_BREAKOUT_OR_PULLBACK_SPEC))
    mask = compiled._entry_mask(df)
    assert bool(mask.iloc[-1]) is True


def test_breakout_or_pullback_fires_on_pause_confirmation_below_original_high():
    """A post-breakout 3-tight-days pause that reclaims the pause's OWN high
    (but not the original breakout's higher high) must still fire — this is
    the case the raw-breakout leg alone would miss."""
    opens, highs, lows, closes, volumes = _flat_baseline_df(20)

    # Breakout day: fresh high well above the flat baseline.
    opens.append(100.0)
    highs.append(116.0)
    lows.append(99.0)
    closes.append(115.0)
    volumes.append(2_000_000.0)

    # 3 tight, low-volume pause days below the breakout high.
    for _ in range(3):
        opens.append(112.0)
        highs.append(113.0)
        lows.append(111.0)
        closes.append(112.0)
        volumes.append(300_000.0)  # well under 0.8x the ~1.05M trailing average

    # Trigger day: closes above the pause cluster's high (113) but BELOW the
    # original breakout's high (116), so the raw breakout_up leg is False here.
    opens.append(113.0)
    highs.append(114.5)
    lows.append(112.5)
    closes.append(114.0)
    volumes.append(1_500_000.0)

    idx = pd.date_range("2021-01-01", periods=25, freq="B")
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )

    compiled = USSCompiler().compile(JSONParser().parse(_BREAKOUT_OR_PULLBACK_SPEC))
    mask = compiled._entry_mask(df)
    assert bool(mask.iloc[-1]) is True  # pullback leg fires


_SMA_HIGH_CROSS_SPEC = {
    "meta": {
        "name": "sma_high_cross_only",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PATTERN", "pattern": "sma_high_cross", "period": 10}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {"trailing_stop": {"method": "sma_band", "period": 10}},
}


def test_sma_high_cross_fires_only_on_the_fresh_cross_bar():
    """close > SMA(high, period) must fire on the FIRST bar that clears the
    band, not on every later bar that stays above it (fresh-cross semantics,
    matching the ORB breakout convention elsewhere in this compiler)."""
    n = 15
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    closes = [100.0] * (n - 2) + [112.0, 113.0]  # cross on bar n-2, stays above on n-1
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1e6] * n},
        index=idx,
    )

    compiled = USSCompiler().compile(JSONParser().parse(_SMA_HIGH_CROSS_SPEC))
    mask = compiled._entry_mask(df)
    assert bool(mask.iloc[-2]) is True  # the fresh-cross bar
    assert bool(mask.iloc[-1]) is False  # already above the band — not a fresh cross


def test_sma_band_trail_exits_when_close_crosses_below_sma_of_lows():
    """The sma_band trailing-stop level for a long must be SMA(low, period),
    not SMA(close, period) — and must only ratchet upward, never loosen."""
    n = 20
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    lows = list(np.linspace(90.0, 100.0, n))  # rising lows -> rising band
    closes = [x + 5 for x in lows]
    highs = [x + 6 for x in lows]
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1e6] * n},
        index=idx,
    )

    compiled = USSCompiler().compile(JSONParser().parse(_SMA_HIGH_CROSS_SPEC))
    expected_band = float(pd.Series(lows).rolling(10).mean().iloc[-1])
    trail_val = compiled.compute_trail(current_sl=0.0, entry=closes[0], df=df, idx=n - 1)
    assert trail_val == pytest.approx(expected_band)


_CLOSE_EXIT_SPEC = {
    "meta": {
        "name": "close_exit",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PRICE", "condition": "price_above", "threshold": 99.5}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 10.0},  # far away — exit only via MA
    "exit_conditions": {
        "trailing_stop": {
            "method": "indicator_cross",
            "indicator": "SMA",
            "period": 5,
            "cross": "price_below_on_close",
        }
    },
}


def test_indicator_cross_exit_is_close_based_not_intraday():
    """An intraday wick below the MA (bar 19) that closes back above must NOT
    exit; only a CLOSE below the MA (bar 20) exits. Distinguishes Minervini's
    close-below-MA rule from an intraday touch."""
    # Bar 19 wicks to 105 (below SMA5~106.4) but closes 107 (above) → no exit.
    # Bar 20 closes 104 (below SMA5~106.1) → exit.
    closes = [99.0] * 10 + [
        100.0,
        101.0,
        102.0,
        103.0,
        104.0,
        105.5,
        106.0,
        106.5,
        107.0,
        107.0,
        104.0,
    ]
    highs = [99.0] * 10 + [
        100.3,
        101.3,
        102.3,
        103.3,
        104.3,
        105.8,
        106.3,
        106.8,
        107.3,
        107.3,
        104.3,
    ]
    lows = [99.0] * 10 + [
        99.7,
        100.7,
        101.7,
        102.7,
        103.7,
        105.2,
        105.7,
        106.2,
        106.7,
        105.0,
        103.7,
    ]
    n = len(closes)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000.0] * n},
        index=idx,
    )
    compiled = USSCompiler().compile(JSONParser().parse(_CLOSE_EXIT_SPEC))
    signals = compiled.generate_backtest_signals(df)

    assert len(signals) >= 1
    assert signals[0].exit_bar == 20  # closed below MA, not the bar-19 wick
    assert signals[0].pnl_r is not None and signals[0].pnl_r > 0


def test_minervini_vcp_builtin_has_rs_gate_and_close_exit():
    """The shipped minervini_vcp spec wires the RS-rating gate and the
    chandelier trail + partial-book/breakeven/reversal profit protection
    (2026-08-21: replaced the looser close-below-50MA exit, which let swing
    winners give back 10%+ before triggering)."""
    import json
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[3]
        / "strategies"
        / "builtin"
        / "minervini_vcp.json"
    )
    spec = JSONParser().parse(json.loads(path.read_text()))

    names = [c.name for c in spec.entry_conditions.conditions]
    assert "rs_rating" in names
    assert spec.exit_conditions.trailing_stop is not None
    assert spec.exit_conditions.trailing_stop.method == "chandelier"
    assert spec.exit_conditions.trailing_stop.chandelier_period == 22
    assert spec.exit_conditions.trailing_stop.chandelier_multiplier == 2.5
    assert spec.exit_conditions.partial_book_r == 1.0
    assert spec.exit_conditions.trail_activation_r == 1.0
    assert spec.exit_conditions.reversal_exit is True


def test_chandelier_trail_reads_its_own_json_keys():
    """The chandelier trail's period/multiplier were previously read from the
    generic "period"/"multiplier" JSON keys (meant for indicator_cross), so a
    JSON-specified chandelier_period/chandelier_multiplier was silently
    dropped and the engine fell back to its hardcoded defaults (22 / 3.0)."""
    spec = JSONParser().parse(
        {
            "meta": {"name": "chandelier_test", "product": "DELIVERY", "timeframe": "DAILY"},
            "universe_filter": {},
            "entry_conditions": {
                "logic": "AND",
                "conditions": [
                    {
                        "name": "above_sma",
                        "type": "INDICATOR",
                        "params": {
                            "indicator": "SMA",
                            "period": 50,
                            "condition": "value_above",
                            "threshold": 0,
                        },
                    }
                ],
            },
            "stop_loss": {"method": "atr_based", "atr_period": 14, "atr_multiplier": 3.0},
            "target": {"method": "risk_reward", "ratio": 2.0},
            "exit_conditions": {
                "trailing_stop": {
                    "method": "chandelier",
                    "chandelier_period": 10,
                    "chandelier_multiplier": 1.75,
                }
            },
        }
    )
    trail = spec.exit_conditions.trailing_stop
    assert trail.chandelier_period == 10
    assert trail.chandelier_multiplier == 1.75


def test_json_parser_loads_orb_intraday_v2_session_market_filter_and_exit_model():
    """Regression: the JSON parser must carry the `intraday` session, the
    `market_filter` direction gate, and the exit-model controls through to the
    spec. orb_intraday v2 (ORB.md) is a 20-bar breakout with an ATR stop, a
    breakeven-then-trail exit (no partial / no reversal), and a NIFTY gate.

    Inlined (rather than read from strategies/builtin/orb_intraday.json) —
    that strategy was retired 2026-07-29 (superseded twice over, by v2 then
    the smart-exit lineage); this fixture preserves the parser regression
    coverage without keeping a dead strategy file live in the registry dirs."""
    orb_intraday_v5_spec: dict[str, object] = {
        "meta": {
            "name": "orb_intraday",
            "display_name": "ORB Intraday v5",
            "version": "5.0",
            "product": "MIS",
            "direction": "BOTH",
            "timeframe": "5MIN",
        },
        "universe_filter": {
            "min_price": 150,
            "min_adv_crores": 100,
            "exchanges": ["NSE"],
            "index_filter": None,
        },
        "entry_conditions": {
            "logic": "AND",
            "conditions": [
                {
                    "name": "breakout_20bar_high",
                    "type": "PRICE",
                    "condition": "price_above",
                    "reference": "consolidation_high",
                    "lookback_days": 20,
                },
                {
                    "name": "volume_surge",
                    "type": "VOLUME",
                    "condition": "volume_ratio_above",
                    "ratio": 1.5,
                    "average_days": 20,
                },
            ],
        },
        "stop_loss": {
            "method": "fixed_pct",
            "fixed_pct": 0.5,
            "atr_period": 14,
            "atr_multiplier": 2.0,
            "max_sl_pct": 3.0,
        },
        "target": {"method": "risk_reward", "ratio": 3.0},
        "exit_conditions": {
            "trailing_stop": {"method": "atr_trail", "atr_period": 14, "atr_multiplier": 2.0},
            "breakeven_at_r": 1.0,
            "trail_activation_r": 1.0,
            "partial_book_r": None,
            "reversal_exit": False,
            "time_stop_days": 0,
        },
        "intraday": {
            "range_window": "0915-0920",
            "entry_window": "0930-1500",
            "exit_window": "1510-1530",
            "timezone": "Asia/Kolkata",
            "max_trades_per_day": 1,
            "cooldown_bars": 10,
        },
        "market_filter": {"symbol": "NIFTY 50", "enabled": True},
    }
    spec = JSONParser().parse(orb_intraday_v5_spec)

    assert spec.intraday is not None
    assert spec.intraday.entry_window == "0930-1500"
    assert spec.intraday.exit_window == "1510-1530"
    # ORB.md risk_method=Percentage → SL = 0.5% of entry (commit 88b8c43 switched
    # from the invalid fixed_points to fixed_pct; compiler dispatches on method).
    assert spec.stop_loss.method == SLMethod.FIXED_PCT
    assert spec.stop_loss.fixed_pct == 0.5
    # Market-direction gate (Pine request.security(NSE:NIFTY)).
    assert spec.market_filter is not None
    assert spec.market_filter.enabled is True
    assert spec.market_filter.symbol == "NIFTY 50"
    # v5: breakeven at +1R; trail activates at +1R; NO partial; no reversal.
    assert spec.exit_conditions.breakeven_at_r == 1.0
    assert spec.exit_conditions.trail_activation_r == 1.0
    assert spec.exit_conditions.partial_book_r is None
    assert spec.exit_conditions.reversal_exit is False


def test_score_counts_long_short_below_min_trades():
    """Regression: `_score` must report long/short split even when the sample is
    below the min-trades gate, so the Strategy Studio card shows the real split
    instead of 0/0 for low-data intraday strategies."""
    from src.strategies.uss.compiler import BacktestSignal
    from src.strategies.uss.schema import Direction

    signals = [
        BacktestSignal(
            bar_index=i,
            date=None,
            entry_price=100.0,
            sl=99.0,
            target=102.0,
            pnl_r=-1.0,
            direction=d,
        )
        for i, d in enumerate([Direction.LONG, Direction.SHORT, Direction.SHORT])
    ]
    result = BacktestEngine()._score("orb_intraday", signals)

    assert result.total_trades == 3
    assert not result.passed
    assert "insufficient" in result.reject_reason
    assert result.long_trades == 1
    assert result.short_trades == 2


# ---------------------------------------------------------------------------
# ORB v2 — market-direction gate + breakeven-then-trail exit model
# ---------------------------------------------------------------------------


def _two_day_5m(bench_d2_close: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A warm-up day then a second day on a shared 5m index. The symbol always
    breaks out on day 2 (close 100 > 99.5). The benchmark closes 100 on day 1
    (→ prev-day close 100) and `bench_d2_close` on day 2."""
    d1 = pd.date_range("2021-01-01 09:15", periods=6, freq="5min")
    d2 = pd.date_range("2021-01-04 09:15", periods=6, freq="5min")
    idx = d1.append(d2)
    n = len(idx)
    sym_close = [99.0] * 6 + [100.0] * 6
    sym = pd.DataFrame(
        {
            "open": sym_close,
            "high": [c + 0.1 for c in sym_close],
            "low": [c - 0.1 for c in sym_close],
            "close": sym_close,
            "volume": [3_000_000.0] * n,
        },
        index=idx,
    )
    bench_close = [100.0] * 6 + [bench_d2_close] * 6
    bench = pd.DataFrame(
        {
            "open": bench_close,
            "high": bench_close,
            "low": bench_close,
            "close": bench_close,
            "volume": [0.0] * n,
        },
        index=idx,
    )
    return sym, bench


_MKT_GATE_SPEC = {
    "meta": {"name": "mkt_gate", "direction": "LONG", "timeframe": "DAILY"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PRICE", "condition": "price_above", "threshold": 99.5}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "market_filter": {"symbol": "NIFTY 50", "enabled": True},
}


def test_market_filter_gates_longs_by_benchmark_direction():
    """Longs are allowed only when the benchmark trades above its previous-day
    close (ORB.md request.security(NSE:NIFTY) gate). Same symbol breakout, two
    benchmark directions → opposite long-mask outcomes on the breakout bar."""
    compiled = USSCompiler().compile(JSONParser().parse(_MKT_GATE_SPEC))

    sym_up, bench_up = _two_day_5m(101.0)  # benchmark > prev-day close
    compiled.set_market_context(bench_up)
    assert bool(compiled._long_mask(sym_up).iloc[-1]) is True

    sym_dn, bench_dn = _two_day_5m(99.0)  # benchmark < prev-day close
    compiled.set_market_context(bench_dn)
    assert bool(compiled._long_mask(sym_dn).iloc[-1]) is False


_BE_EXIT_SPEC = {
    "meta": {"name": "be_exit", "direction": "LONG", "timeframe": "DAILY"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PRICE", "condition": "price_above", "threshold": 99.5}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 10.0},  # far — exit via trail, not TP
    "exit_conditions": {
        "trailing_stop": {"method": "atr_trail", "atr_period": 14, "atr_multiplier": 1.5},
        "breakeven_at_r": 1.0,
        "trail_activation_r": 0.0,
        "partial_book_r": None,
        "reversal_exit": False,
    },
}


def test_breakeven_then_trail_protects_winner_without_partial_booking():
    """ORB.md v2 exit model: a trade that runs past +1R then pulls back must NOT
    give back to a full -1R loss (breakeven + continuous trail protect it), and
    must NOT book a 50% partial (partial_book_r is off → partial_r stays None)."""
    closes = [99.0] * 14 + [100.0, 102.0, 104.0, 106.0, 108.0, 104.0, 100.5]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    n = len(closes)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000.0] * n},
        index=idx,
    )
    compiled = USSCompiler().compile(JSONParser().parse(_BE_EXIT_SPEC))
    signals = compiled.generate_backtest_signals(df)

    assert len(signals) >= 1
    assert signals[0].partial_r is None  # no 50% partial book
    assert signals[0].pnl_r is not None
    assert signals[0].pnl_r > -0.1  # breakeven/trail protected the run-up


_PARTIAL_2R_SPEC = {
    "meta": {"name": "partial_2r", "direction": "LONG", "timeframe": "DAILY"},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [
            {"name": "entry", "type": "PRICE", "condition": "price_above", "threshold": 99.5}
        ],
    },
    "stop_loss": {"method": "fixed_pct", "fixed_pct": 5.0, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 5.0},
    "exit_conditions": {
        "trailing_stop": {"method": "atr_trail", "atr_period": 14, "atr_multiplier": 1.5},
        "breakeven_at_r": 1.0,
        "trail_activation_r": 1.0,
        "partial_book_r": 2.0,
        "reversal_exit": False,
    },
}


def test_partial_at_2r_then_trail_second_half():
    """Exit 50% at +2R, trail the second half from +1R.
    pnl_r = 0.5×2.0 + 0.5×trailing_exit > 1.5 when trail exits above entry.
    Trail is CLOSE-based (ORB.md Pine): the final bar pulls back to 105 so its low
    crosses the ratcheted close-trail stop and the second half exits as Trail SL."""
    closes = [99.0] * 14 + [100.0, 102.0, 104.0, 105.0, 107.0, 109.0, 111.0, 105.0]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    n = len(closes)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    df = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000.0] * n},
        index=idx,
    )
    compiled = USSCompiler().compile(JSONParser().parse(_PARTIAL_2R_SPEC))
    signals = compiled.generate_backtest_signals(df)

    assert len(signals) >= 1
    sig = signals[0]
    assert sig.partial_r == 0.5, "50% should be booked at +2R"
    assert sig.pnl_r is not None
    # booked half = +1.0R; trail exits above entry → total > 1.5R
    assert sig.pnl_r > 1.5, f"expected pnl_r > 1.5, got {sig.pnl_r}"
    assert sig.exit_reason in {"Trail SL", "EOD", "Target Hit", "Time Stop"}


# ---------------------------------------------------------------------------
# BacktestEngine cost model
# ---------------------------------------------------------------------------


def _cost_signals(n: int = 30, pnl_r: float = 1.0) -> list:
    from src.strategies.uss.compiler import BacktestSignal

    # entry 100, sl 99 → risk = 1% of notional
    return [
        BacktestSignal(
            bar_index=i, date=None, entry_price=100.0, sl=99.0, target=102.0, pnl_r=pnl_r
        )
        for i in range(n)
    ]


def test_cost_model_zero_is_noop():
    signals = _cost_signals(pnl_r=1.0)
    res = BacktestEngine()._score("s", signals)
    assert res.expectancy_r == pytest.approx(1.0)


def test_cost_model_subtracts_cost_in_r_units():
    # 0.10% round-trip cost on a 1%-risk trade = 0.1R per trade
    signals = _cost_signals(pnl_r=1.0)
    res = BacktestEngine(cost_pct=0.10)._score("s", signals)
    assert res.expectancy_r == pytest.approx(0.9)


def test_cost_model_degenerate_stop_capped_at_one_r():
    from src.strategies.uss.compiler import BacktestSignal

    sig = BacktestSignal(
        bar_index=0, date=None, entry_price=100.0, sl=100.0, target=102.0, pnl_r=1.0
    )
    assert BacktestEngine(cost_pct=0.10)._cost_r(sig) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# opening_range_width condition
# ---------------------------------------------------------------------------


def _width_condition(**params):
    from src.strategies.uss.schema import Condition, ConditionType

    return Condition(
        name="or_width",
        type=ConditionType.PRICE,
        params={"condition": "value_between", "reference": "opening_range_width", **params},
    )


def test_opening_range_width_filters_by_bounds():
    from src.strategies.uss.compiler import ConditionCompiler

    n = 4
    idx = pd.date_range("2026-01-05 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1e6] * n,
        },
        index=idx,
    )
    # day's OR width: 0.2% → inside [0.1, 1.5]; 3.0% → outside
    orb_high_ok = pd.Series(100.2, index=idx)
    orb_low = pd.Series(100.0, index=idx)
    cc = ConditionCompiler(orb_high=orb_high_ok, orb_low=orb_low)
    assert cc.compile_series(_width_condition(min_pct=0.1, max_pct=1.5), df).all()

    orb_high_wide = pd.Series(103.0, index=idx)
    cc_wide = ConditionCompiler(orb_high=orb_high_wide, orb_low=orb_low)
    assert not cc_wide.compile_series(_width_condition(min_pct=0.1, max_pct=1.5), df).any()
    # too-narrow (dead) range fails min_pct
    assert not cc.compile_series(_width_condition(min_pct=0.5), df).any()


def test_market_filter_regime_sma_gates_by_benchmark_trend():
    """regime_sma mode: longs allowed only while the benchmark closes above its
    N-bar SMA (fail-closed while the SMA is still warming up)."""
    import copy

    spec = copy.deepcopy(_MKT_GATE_SPEC)
    spec["market_filter"] = {
        "symbol": "NIFTY 50",
        "enabled": True,
        "mode": "regime_sma",
        "sma_period": 5,
    }
    compiled = USSCompiler().compile(JSONParser().parse(spec))

    n = 12
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    sym = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1e6] * n,
        },
        index=idx,
    )

    def bench(closes):
        return pd.DataFrame(
            {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [0.0] * n},
            index=idx,
        )

    # Rising benchmark: last close above its 5-bar SMA → longs allowed
    compiled.set_market_context(bench(list(range(100, 100 + n))))
    assert bool(compiled._long_mask(sym).iloc[-1]) is True
    # Falling benchmark: last close below its SMA → longs blocked
    compiled.set_market_context(bench(list(range(100 + n, 100, -1))))
    assert bool(compiled._long_mask(sym).iloc[-1]) is False
    # SMA warmup (first bars): fail-closed
    compiled.set_market_context(bench(list(range(100, 100 + n))))
    assert bool(compiled._long_mask(sym).iloc[2]) is False


def test_opening_range_width_false_without_session():
    from src.strategies.uss.compiler import ConditionCompiler

    df = _make_trending_df(30)
    mask = ConditionCompiler().compile_series(_width_condition(min_pct=0.1, max_pct=1.5), df)
    assert not mask.any()


# ---------------------------------------------------------------------------
# BETA indicator
# ---------------------------------------------------------------------------


def test_beta_matches_known_ratio():
    from src.strategies.indicators.library import compute

    rng = np.random.default_rng(7)
    n = 300
    bench_rets = rng.normal(0.0005, 0.01, n)
    bench = pd.Series(1000 * np.cumprod(1 + bench_rets))
    # Stock built to move at exactly 1.5x the benchmark's daily returns.
    stock_rets = bench_rets * 1.5
    stock_close = pd.Series(500 * np.cumprod(1 + stock_rets))
    df = pd.DataFrame({"close": stock_close})

    result = compute(df, "BETA", period=252, benchmark=bench)
    assert abs(result.iloc[-1] - 1.5) < 0.05


def test_beta_unknown_without_benchmark_is_nan():
    from src.strategies.indicators.library import compute

    df = pd.DataFrame({"close": pd.Series(np.linspace(100, 110, 300))})
    result = compute(df, "BETA", period=252)
    assert result.iloc[-1] != result.iloc[-1]  # NaN


# ---------------------------------------------------------------------------
# Wyckoff spring / LPS patterns
# ---------------------------------------------------------------------------


def test_wyckoff_spring_fires_on_undercut_reclaim():
    from src.strategies.uss.compiler import ConditionCompiler
    from src.strategies.uss.schema import Condition, ConditionType

    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 100.0)
    volume = np.full(n, 1_000_000.0)
    low = close - 1.0
    high = close + 1.0

    # Bars 0-49: flat range, low never below 99 (close=100, low=close-1).
    # Bar 55: spring — undercuts the established range low on LOW volume
    # (below 0.8x avg) — a shakeout, not real supply.
    low[55] = 90.0
    high[55] = 92.0
    close[55] = 91.0
    volume[55] = 300_000.0
    # Bar 60 onward: reclaim — closes back ABOVE the established range low
    # (~99), confirming the undercut was a shakeout.
    close[60:] = 99.5
    high[60:] = 100.5
    low[60:] = 99.0

    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )
    cond = Condition(
        name="spring",
        type=ConditionType.PATTERN,
        params={
            "pattern": "wyckoff_spring",
            "range_lookback": 40,
            "spring_window": 10,
            "undercut_pct": 1.0,
            "max_volume_ratio": 0.8,
            "avg_days": 20,
        },
    )
    result = ConditionCompiler().compile_series(cond, df)
    # rolling(spring_window) looks BACKWARD from the current bar, so the spring
    # (bar 55) is only "in window" for bars 55 through 55+spring_window-1=64.
    assert bool(result.iloc[63]) is True  # within spring_window of bar 55, reclaimed
    assert bool(result.iloc[52]) is False  # before the spring happened


def test_wyckoff_spring_does_not_fire_on_high_volume_undercut():
    from src.strategies.uss.compiler import ConditionCompiler
    from src.strategies.uss.schema import Condition, ConditionType

    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 100.0)
    volume = np.full(n, 1_000_000.0)
    low = close - 1.0
    high = close + 1.0

    low[55] = 90.0
    close[55] = 91.0
    volume[55] = 2_000_000.0  # HIGH volume undercut — real breakdown, not a spring
    close[60:] = 99.0
    low[60:] = 98.5

    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )
    cond = Condition(
        name="spring",
        type=ConditionType.PATTERN,
        params={
            "pattern": "wyckoff_spring",
            "range_lookback": 40,
            "spring_window": 10,
            "undercut_pct": 1.0,
            "max_volume_ratio": 0.8,
            "avg_days": 20,
        },
    )
    result = ConditionCompiler().compile_series(cond, df)
    assert bool(result.iloc[65]) is False


def test_wyckoff_lps_fires_on_quiet_pullback_after_confirmed_breakout():
    from src.strategies.uss.compiler import ConditionCompiler
    from src.strategies.uss.schema import Condition, ConditionType

    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 100.0)
    high = close + 0.5
    low = close - 0.5
    volume = np.full(n, 1_000_000.0)

    # Bars 0-49: base, resistance established at ~100.5 (high).
    # Bar 55: confirmed SOS breakout, closes 4% above resistance.
    close[55] = 104.5
    high[55] = 105.0
    low[55] = 103.5
    volume[55] = 2_500_000.0
    close[56:63] = 104.0
    # Bar 63: LPS — quiet pullback close to the old resistance (~100.5), low volume.
    close[63] = 101.5
    high[63] = 102.0
    low[63] = 101.0
    volume[63] = 400_000.0
    close[64:] = 106.0  # resumes higher after the LPS

    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )
    cond = Condition(
        name="lps",
        type=ConditionType.PATTERN,
        params={
            "pattern": "wyckoff_lps",
            "range_lookback": 40,
            "breakout_lookback": 15,
            "pullback_window": 8,
            "breakout_min_pct": 2.0,
            "max_pullback_pct": 5.0,
            "max_volume_ratio": 0.9,
            "avg_days": 20,
        },
    )
    result = ConditionCompiler().compile_series(cond, df)
    assert bool(result.iloc[63]) is True
    assert bool(result.iloc[56]) is False  # too soon, no pullback yet


def test_wyckoff_lps_does_not_fire_without_prior_breakout():
    from src.strategies.uss.compiler import ConditionCompiler
    from src.strategies.uss.schema import Condition, ConditionType

    n = 80
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = np.full(n, 100.0)
    high = close + 0.5
    low = close - 0.5
    volume = np.full(n, 1_000_000.0)
    # Quiet low-volume dip with NO prior breakout in the window.
    close[63] = 99.0
    volume[63] = 400_000.0

    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )
    cond = Condition(
        name="lps",
        type=ConditionType.PATTERN,
        params={
            "pattern": "wyckoff_lps",
            "range_lookback": 40,
            "breakout_lookback": 15,
            "pullback_window": 8,
            "breakout_min_pct": 2.0,
            "max_pullback_pct": 5.0,
            "max_volume_ratio": 0.9,
            "avg_days": 20,
        },
    )
    result = ConditionCompiler().compile_series(cond, df)
    assert bool(result.iloc[63]) is False


def test_json_parser_reads_min_sl_pct():
    """Regression: _parse_sl() silently dropped min_sl_pct, so every strategy's
    SL floor (StopLoss.min_sl_pct, enforced by compiler._compute_sl_value's
    _clamp()) fell back to the schema default of 0.0 regardless of what the
    JSON specified — found 2026-08-13 tracing why an orb_intraday_smart trade's
    stop sat at 0.03% from entry despite min_sl_pct=0.35 in the spec."""
    spec = JSONParser().parse(
        _VCP_JSON | {"stop_loss": {**_VCP_JSON["stop_loss"], "min_sl_pct": 0.35}}
    )
    assert spec.stop_loss.min_sl_pct == 0.35
