"""The intraday LLM prompt carries RSI and relative strength vs the market (2026-09-24).

Before: per stock it saw price, % move, gap, VWAP distance, opening range, day range, volume
ratio, ATR and EMA slope -- no RSI and no relative strength; the Nifty's move was only given at
market level and the model had to do the subtraction itself.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from src.strategies.llm_intraday.pipeline import (
    ValidationLimits,
    add_relative_strength,
    build_symbol_features,
    build_system_prompt,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 9, 11, 10, 30, tzinfo=_IST)


def _df(today_closes: list[float]) -> pd.DataFrame:
    y_idx = pd.date_range("2026-09-10 12:45", periods=30, freq="5min", tz=_IST)
    t_idx = pd.date_range("2026-09-11 09:15", periods=len(today_closes), freq="5min", tz=_IST)
    rows = [(100.0, 100.2, 99.8, 100.0)] * 30
    for c in today_closes:
        rows.append((c - 0.3, c + 0.2, c - 0.4, c))
    o, h, lo, c = zip(*rows, strict=True)
    return pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": c, "volume": [10_000.0] * len(rows)},
        index=y_idx.append(t_idx),
    )


def test_features_include_a_5m_rsi_that_reads_high_after_a_run_up_and_low_after_a_sell_off() -> (
    None
):
    up = build_symbol_features(_df([100.5 + 0.5 * i for i in range(12)]), _NOW)
    down = build_symbol_features(_df([99.5 - 0.5 * i for i in range(12)]), _NOW)
    assert up is not None and down is not None
    assert up["rsi14"] is not None and up["rsi14"] > 70
    assert down["rsi14"] is not None and down["rsi14"] < 30


def test_relative_strength_is_stock_move_minus_the_index_move() -> None:
    feats = {"AAA": {"chg_pct": -1.0}, "BBB": {"chg_pct": -2.5}, "CCC": {"chg_pct": 0.3}}
    add_relative_strength(feats, nifty_chg_pct=-1.2)
    # in a -1.2% index tape, a -1.0% stock is OUTperforming, a -2.5% stock is lagging
    assert feats["AAA"]["rs_vs_nifty_pct"] == 0.2
    assert feats["BBB"]["rs_vs_nifty_pct"] == -1.3
    assert feats["CCC"]["rs_vs_nifty_pct"] == 1.5


def test_relative_strength_is_omitted_not_faked_when_the_index_move_is_unknown() -> None:
    feats = {"AAA": {"chg_pct": -1.0}}
    add_relative_strength(feats, nifty_chg_pct=None)
    assert "rs_vs_nifty_pct" not in feats["AAA"]


def test_system_prompt_uses_relative_strength_but_not_rsi() -> None:
    text = build_system_prompt(ValidationLimits())
    assert "rs_vs_nifty_pct" in text
    assert "rsi" not in text.lower()  # no edge on 259 closed MIS trades: kept out of the prompt


def test_rsi_is_withheld_from_the_prompt_context_but_still_recorded() -> None:
    from src.strategies.llm_intraday.pipeline import build_market_context

    feats = {"AAA": {"ltp": 100.0, "chg_pct": -1.0, "rsi14": 22.0}}
    add_relative_strength(feats, nifty_chg_pct=-1.2)
    recorded = {s: f.pop("rsi14", None) for s, f in feats.items()}  # what the pass does
    ctx = build_market_context(
        nifty_chg_pct=-1.2,
        intraday_regime="BEAR",
        daily_regime="BEAR",
        bearish_pct=70.0,
        vix=12.0,
        sector_status={},
        features=feats,
        universe=["AAA"],
        news={},
        minutes_to_flat=120,
    )
    assert recorded == {"AAA": 22.0}
    assert "rsi14" not in ctx["candidate_universe"][0]
    assert ctx["candidate_universe"][0]["rs_vs_nifty_pct"] == 0.2


# ── timing / extension awareness (2026-09-24, AXISBANK sold 92% into its day's drop) ──────


def test_features_show_how_much_room_is_left_to_the_days_extremes() -> None:
    feats = build_symbol_features(_df([99.0, 98.0, 97.0, 96.5, 96.2, 96.0]), _NOW)
    assert feats is not None
    ltp, day_low, day_high = feats["ltp"], feats["day_low"], feats["day_high"]
    assert feats["off_day_low_pct"] == round((ltp - day_low) / ltp * 100, 2)
    assert feats["off_day_high_pct"] == round((day_high - ltp) / ltp * 100, 2)
    # a stock sitting right on its low after a big drop: almost no room left for a SHORT
    assert feats["off_day_low_pct"] < 0.5 < feats["off_day_high_pct"]


def test_system_prompt_warns_against_chasing_an_exhausted_move() -> None:
    text = build_system_prompt(ValidationLimits()).lower()
    assert "off_day_low_pct" in text and "off_day_high_pct" in text
    assert "exhausted" in text or "already spent" in text
