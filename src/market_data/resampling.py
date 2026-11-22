"""OHLCV timeframe resampling: daily -> monthly, 15-minute -> 30-minute.

Neither monthly nor 30-minute candles are stored anywhere (ohlcv_downloader.py
only persists 1d/1w/15m/10m/5m) — strategies that need those timeframes must
resample from what's stored, at read time.

`include_partial=False` (the default) drops the in-progress final period. This
matters for point-in-time backtesting: a mid-month or mid-30-minutes bar's
high/low/close is not yet "real" and including it would leak forward-looking
information into a decision made before the period closed. Live scanning
should pass `include_partial=True` so "the current bar so far" is visible.
"""

from __future__ import annotations

import pandas as pd

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample(df: pd.DataFrame, rule: str, *, include_partial: bool) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = df.resample(rule).agg(_AGG).dropna(how="all")
    if not include_partial and len(out) > 0:
        out = out.iloc[:-1]
    return out


def resample_daily_to_monthly(
    df: pd.DataFrame, *, include_partial_month: bool = False
) -> pd.DataFrame:
    """Daily OHLCV (DatetimeIndex) -> calendar-month OHLCV.

    open=first, high=max, low=min, close=last, volume=sum per month.
    """
    return _resample(df, "ME", include_partial=include_partial_month)


def resample_15m_to_30m(df: pd.DataFrame, *, include_partial_bar: bool = False) -> pd.DataFrame:
    """15-minute OHLCV (DatetimeIndex) -> 30-minute OHLCV."""
    return _resample(df, "30min", include_partial=include_partial_bar)
