"""
Indicator library — pure pandas/numpy implementations (no pandas_ta / numba).

All functions accept a DataFrame with columns: open, high, low, close, volume.
All functions return a Series aligned to the input index.
Callers receive NaN for the warm-up period — never fill or assume values.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------


def sma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].rolling(period).mean()


def ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def wma(df: pd.DataFrame, period: int) -> pd.Series:
    weights = np.arange(1, period + 1, dtype=float)
    w_sum = weights.sum()
    return df["close"].rolling(period).apply(lambda x: float(np.dot(x, weights) / w_sum), raw=True)


def hma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    half = max(1, period // 2)
    sqrt_n = max(1, int(period**0.5))
    raw = 2 * wma(df, half) - wma(df, period)
    tmp = df.copy()
    tmp["close"] = raw
    return wma(tmp, sqrt_n)


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def supertrend(df: pd.DataFrame, period: int = 10, **kwargs: Any) -> pd.Series:
    mult = float(kwargs.get("multiplier", 3.0))
    a = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * a
    lower = hl2 - mult * a

    direction = pd.Series(1, index=df.index, dtype=float)
    final_upper = upper.copy()
    final_lower = lower.copy()

    for i in range(1, len(df)):
        fu_prev = final_upper.iloc[i - 1]
        fl_prev = final_lower.iloc[i - 1]
        c_prev = df["close"].iloc[i - 1]

        final_upper.iloc[i] = (
            upper.iloc[i] if upper.iloc[i] < fu_prev or c_prev > fu_prev else fu_prev
        )
        final_lower.iloc[i] = (
            lower.iloc[i] if lower.iloc[i] > fl_prev or c_prev < fl_prev else fl_prev
        )

        if direction.iloc[i - 1] == 1:
            direction.iloc[i] = -1.0 if df["close"].iloc[i] < final_lower.iloc[i] else 1.0
        else:
            direction.iloc[i] = 1.0 if df["close"].iloc[i] > final_upper.iloc[i] else -1.0

    return direction


def ichimoku(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    """Returns Ichimoku Span A (leading span A)."""
    conv = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    base = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    return ((conv + base) / 2).shift(26)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(df: pd.DataFrame, period: int = 12, **kwargs: Any) -> pd.Series:
    fast = period
    slow = int(kwargs.get("slow", period * 2))
    fast_ema = df["close"].ewm(span=fast, adjust=False).mean()
    slow_ema = df["close"].ewm(span=slow, adjust=False).mean()
    return fast_ema - slow_ema


def stoch(df: pd.DataFrame, period: int = 14, **kwargs: object) -> pd.Series:
    low_n = df["low"].rolling(period).min()
    high_n = df["high"].rolling(period).max()
    denom = (high_n - low_n).replace(0, np.nan)
    return (df["close"] - low_n) / denom * 100


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma_tp = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))


def mom(df: pd.DataFrame, period: int = 10) -> pd.Series:
    return df["close"] - df["close"].shift(period)


def roc(df: pd.DataFrame, period: int = 10) -> pd.Series:
    prev = df["close"].shift(period).replace(0, np.nan)
    return (df["close"] - prev) / prev * 100


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_pc = (df["high"] - df["close"].shift(1)).abs()
    low_pc = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def plus_di(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder +DI: smoothed upward directional movement as % of smoothed true range."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    return 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr(df, period)


def weekly_max_low(df: pd.DataFrame, period: int = 52) -> pd.Series:
    """Highest weekly low over the last `period` weeks (Mon-Sun weeks), point-in-time.

    The in-progress week contributes only its running low up to each row, and completed
    weeks their final low — so a row never sees a later day's low (a plain
    resample("W") reindexed back onto daily rows would leak it into backtests).
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        # pd.DatetimeIndex(int_index) would silently read ints as 1970 nanoseconds
        # and return NaN everywhere — fail loudly so the caller logs/alerts instead.
        raise ValueError(f"WEEKLY_MAX_LOW needs a DatetimeIndex, got {type(df.index).__name__}")
    idx = df.index
    if idx.tz is not None:
        idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
    codes = pd.factorize(idx.to_period("W"))[0]
    running = df["low"].groupby(codes).cummin()
    week_min = running.groupby(codes).min()
    prior = week_min.shift(1).rolling(period - 1).max()
    prior_rows = pd.Series(prior.reindex(codes).to_numpy(), index=df.index)
    return np.maximum(prior_rows, running)


def bbands(df: pd.DataFrame, period: int = 20, **kwargs: object) -> pd.Series:
    """Returns middle band (SMA)."""
    return df["close"].rolling(period).mean()


def keltner(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Returns middle band (EMA)."""
    return df["close"].ewm(span=period, adjust=False).mean()


def adrpct(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return ((df["high"] - df["low"]) / df["low"].replace(0, np.nan) * 100).rolling(period).mean()


def hist_vol(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["close"].pct_change().rolling(period).std() * (252**0.5) * 100


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    return df["volume"].rolling(period).mean()


def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    avg = volume_sma(df, period)
    return df["volume"] / avg.replace(0, np.nan)


def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    cum_tp_vol = (tp * df["volume"]).cumsum()
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return cum_tp_vol / cum_vol


def obv(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum().replace(0, np.nan)
    mfr = pos_sum / neg_sum
    return 100 - (100 / (1 + mfr))


def cmf(df: pd.DataFrame, period: int = 20) -> pd.Series:
    hl = (df["high"] - df["low"]).replace(0, np.nan)
    mfv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl * df["volume"]
    vol_sum = df["volume"].rolling(period).sum().replace(0, np.nan)
    return mfv.rolling(period).sum() / vol_sum


# ---------------------------------------------------------------------------
# Price structure
# ---------------------------------------------------------------------------


def highest_high(df: pd.DataFrame, period: int) -> pd.Series:
    return df["high"].rolling(period).max()


def lowest_low(df: pd.DataFrame, period: int) -> pd.Series:
    return df["low"].rolling(period).min()


def range_pct(df: pd.DataFrame, period: int) -> pd.Series:
    hh = highest_high(df, period)
    ll = lowest_low(df, period)
    return (hh - ll) / ll.replace(0, np.nan) * 100


def pivot_high(df: pd.DataFrame, period: int = 5) -> pd.Series:
    result = pd.Series(0.0, index=df.index)
    highs = df["high"].values
    for i in range(period, len(highs) - period):
        window = highs[i - period : i + period + 1]
        if highs[i] == window.max():
            result.iloc[i] = 1.0
    return result


def pivot_low(df: pd.DataFrame, period: int = 5) -> pd.Series:
    result = pd.Series(0.0, index=df.index)
    lows = df["low"].values
    for i in range(period, len(lows) - period):
        window = lows[i - period : i + period + 1]
        if lows[i] == window.min():
            result.iloc[i] = 1.0
    return result


def consol_high(df: pd.DataFrame, period: int = 21) -> pd.Series:
    return df["high"].rolling(period).max()


def consol_low(df: pd.DataFrame, period: int = 21) -> pd.Series:
    return df["low"].rolling(period).min()


# ---------------------------------------------------------------------------
# Pattern indicators (1.0 = detected, 0.0 = not)
# ---------------------------------------------------------------------------


def vcp(df: pd.DataFrame, period: int = 21, **kwargs: Any) -> pd.Series:
    max_range = float(kwargs.get("max_range_pct", 8.0))
    return (range_pct(df, period) < max_range).astype(float)


def stage2(df: pd.DataFrame, period: int = 200) -> pd.Series:
    ma = sma(df, period)
    return ((df["close"] > ma) & (ma > ma.shift(20))).astype(float)


def flat_base(df: pd.DataFrame, period: int = 30) -> pd.Series:
    return (range_pct(df, period) < 15.0).astype(float)


def tight_area(df: pd.DataFrame, period: int = 10) -> pd.Series:
    return (range_pct(df, period) < 5.0).astype(float)


def orb(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    """ORB placeholder — requires intraday data; always 0 on daily OHLCV."""
    return pd.Series(0.0, index=df.index)


def cup_pattern(df: pd.DataFrame, period: int = 50) -> pd.Series:
    prior_high = df["high"].rolling(period).max().shift(period // 2)
    correction = (prior_high - df["low"].rolling(period // 2).min()) / prior_high.replace(0, np.nan)
    return ((df["close"] >= prior_high * 0.95) & (correction >= 0.25)).astype(float)


# ---------------------------------------------------------------------------
# Relative strength
# ---------------------------------------------------------------------------


def rs_line(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    benchmark = kwargs.get("benchmark")
    if not isinstance(benchmark, pd.Series):
        return pd.Series(float("nan"), index=df.index)
    return df["close"] / benchmark.reindex(df.index)


def rs_percentile(df: pd.DataFrame, period: int = 252, **kwargs: object) -> pd.Series:
    """Minervini RS rating proxy — percentile of the relative-strength line.

    The RS line is the stock's price divided by the benchmark (index). Ranking it
    over the trailing `period` gives a 0-100 reading where high = the stock is
    outperforming the market near the top of its own RS trend (true relative
    strength). When no benchmark is supplied we FALL OPEN to ranking the stock's
    own price (a weaker proxy) so signals are never silently zeroed.
    """
    benchmark = kwargs.get("benchmark")
    if isinstance(benchmark, pd.Series) and len(benchmark) > 0:
        bench = benchmark.reindex(df.index).ffill().bfill()
        rs_line = df["close"] / bench.replace(0, float("nan"))
        if rs_line.notna().any():
            return rs_line.rolling(period).rank(pct=True) * 100
    return df["close"].rolling(period).rank(pct=True) * 100


def beta(df: pd.DataFrame, period: int = 252, **kwargs: object) -> pd.Series:
    """Rolling beta of this symbol's returns vs the benchmark's returns.

    beta = cov(stock_returns, bench_returns) / var(bench_returns) over `period`
    trailing bars. NaN wherever no benchmark is supplied — unlike RS_PERCENTILE,
    beta has no meaningful self-referential fallback (a stock's beta against
    itself is always 1.0, which would silently pass every universe filter).
    """
    benchmark = kwargs.get("benchmark")
    if not isinstance(benchmark, pd.Series) or len(benchmark) == 0:
        return pd.Series(float("nan"), index=df.index)
    bench = benchmark.reindex(df.index).ffill().bfill()
    stock_ret = df["close"].pct_change()
    bench_ret = bench.pct_change()
    cov = stock_ret.rolling(period).cov(bench_ret)
    var = bench_ret.rolling(period).var()
    return cov / var.replace(0, float("nan"))


def rs_slope(df: pd.DataFrame, period: int = 20) -> pd.Series:
    ratio = df["close"] / sma(df, period)
    return ratio - ratio.shift(period)


def sector_rs(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    return pd.Series(float("nan"), index=df.index)


# ---------------------------------------------------------------------------
# Composite (Minervini / CANSLIM / Quality)
# ---------------------------------------------------------------------------


def trend_template(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    ma50 = sma(df, 50)
    ma150 = sma(df, 150)
    ma200 = sma(df, 200)
    c1 = df["close"] > ma150
    c2 = df["close"] > ma200
    c3 = ma150 > ma200
    c4 = ma200 > ma200.shift(20)
    c5 = df["close"] > ma50
    c6 = ma50 > ma150
    c7 = df["close"] >= df["close"].rolling(252).min() * 1.25
    c8 = df["close"] >= df["close"].rolling(252).max() * 0.75
    return (c1 & c2 & c3 & c4 & c5 & c6 & c7 & c8).astype(float)


def canslim_score(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    above_200 = (df["close"] > sma(df, 200)).astype(float)
    vol_exp = (volume_ratio(df, 50) > 1.25).astype(float)
    rs_str = (rs_percentile(df, 252) > 70).astype(float)
    return (above_200 + vol_exp + rs_str) / 3.0 * 100


def quality_score(df: pd.DataFrame, **kwargs: object) -> pd.Series:
    trend = (df["close"] > sma(df, 200)).astype(float)
    low_vol = (adrpct(df, 20) < 3.0).astype(float)
    vol_consist = (volume_ratio(df, 20).between(0.5, 2.0)).astype(float)
    return (trend + low_vol + vol_consist) / 3.0 * 100


# ---------------------------------------------------------------------------
# Dispatcher — used by ConditionCompiler
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, object] = {
    # Trend
    "SMA": sma,
    "EMA": ema,
    "WMA": wma,
    "HMA": hma,
    "VWAP": vwap,
    "SUPERTREND": supertrend,
    "ICHIMOKU": ichimoku,
    # Momentum
    "RSI": rsi,
    "MACD": macd,
    "STOCH": stoch,
    "CCI": cci,
    "MOM": mom,
    "ROC": roc,
    # Volatility
    "ATR": atr,
    "PLUS_DI": plus_di,
    "WEEKLY_MAX_LOW": weekly_max_low,
    "BB": bbands,
    "KC": keltner,
    "ADRPCT": adrpct,
    "HIST_VOL": hist_vol,
    # Volume
    "OBV": obv,
    "VOLUME_SMA": volume_sma,
    "VOLUME_RATIO": volume_ratio,
    "MFI": mfi,
    "CMF": cmf,
    # Price
    "HIGHEST": highest_high,
    "LOWEST": lowest_low,
    "HIGHEST_HIGH": highest_high,
    "LOWEST_LOW": lowest_low,
    "RANGE_PCT": range_pct,
    "PIVOT_HIGH": pivot_high,
    "PIVOT_LOW": pivot_low,
    "CONSOL_HIGH": consol_high,
    "CONSOL_LOW": consol_low,
    # Pattern
    "VCP": vcp,
    "STAGE": stage2,
    "CUP": cup_pattern,
    "FLAT_BASE": flat_base,
    "TIGHT_AREA": tight_area,
    "ORB": orb,
    # Relative strength
    "RS_LINE": rs_line,
    "RS_PERCENTILE": rs_percentile,
    "RS_SLOPE": rs_slope,
    "SECTOR_RS": sector_rs,
    "BETA": beta,
    # Composite
    "TREND_TEMPLATE": trend_template,
    "CANSLIM_SCORE": canslim_score,
    "QUALITY_SCORE": quality_score,
}


def compute(df: pd.DataFrame, indicator: str, **kwargs: object) -> pd.Series:
    """Compute a named indicator on df. kwargs forwarded to the function."""
    key = indicator.upper()
    fn = _DISPATCH.get(key)
    if fn is None:
        raise ValueError(f"Unknown indicator: {indicator!r}. Known: {sorted(_DISPATCH)}")
    return fn(df, **kwargs)  # type: ignore[operator]
