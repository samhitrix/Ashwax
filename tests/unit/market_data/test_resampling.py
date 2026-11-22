"""Unit tests for daily->monthly and 15m->30m resampling."""

from __future__ import annotations

import pandas as pd
from src.market_data.resampling import resample_15m_to_30m, resample_daily_to_monthly


def _daily_df(start: str, days: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=days, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(days)],
            "high": [101.0 + i for i in range(days)],
            "low": [99.0 + i for i in range(days)],
            "close": [100.5 + i for i in range(days)],
            "volume": [1000.0 + i for i in range(days)],
        },
        index=idx,
    )


def test_daily_to_monthly_aggregates_ohlcv_correctly():
    df = _daily_df("2026-01-01", 62)  # spans Jan, Feb, part of Mar
    monthly = resample_daily_to_monthly(df, include_partial_month=True)
    jan = df.loc["2026-01-01":"2026-01-31"]
    jan_row = monthly.iloc[0]
    assert jan_row["open"] == jan["open"].iloc[0]
    assert jan_row["high"] == jan["high"].max()
    assert jan_row["low"] == jan["low"].min()
    assert jan_row["close"] == jan["close"].iloc[-1]
    assert jan_row["volume"] == jan["volume"].sum()


def test_daily_to_monthly_excludes_partial_current_month_by_default():
    df = _daily_df("2026-01-01", 45)  # into Feb but not a full Feb
    with_partial = resample_daily_to_monthly(df, include_partial_month=True)
    without_partial = resample_daily_to_monthly(df, include_partial_month=False)
    assert len(without_partial) == len(with_partial) - 1
    assert without_partial.index[-1] < with_partial.index[-1]


def test_15m_to_30m_handles_odd_trailing_bar_without_error():
    idx = pd.date_range("2026-01-01 09:15", periods=5, freq="15min")
    df = pd.DataFrame(
        {
            "open": [1, 2, 3, 4, 5],
            "high": [1, 2, 3, 4, 5],
            "low": [1, 2, 3, 4, 5],
            "close": [1, 2, 3, 4, 5],
            "volume": [10, 10, 10, 10, 10],
        },
        index=idx,
    )
    out = resample_15m_to_30m(df, include_partial_bar=True)
    assert len(out) >= 2


def test_empty_input_returns_empty_dataframe_not_exception():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    empty.index = pd.DatetimeIndex([])
    assert resample_daily_to_monthly(empty).empty
    assert resample_15m_to_30m(empty).empty
