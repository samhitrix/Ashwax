# ruff: noqa: S311, B007, B023, F841  -- one-off research script (bootstrap sampling, loop closures used immediately); logic left untouched so recorded results stay reproducible
"""
Hypothesis G pre-registered test (docs/hypotheses/G_options_iv_divergence.md).

For each stock in options_iv_history with >=30 prior daily IV snapshots: flag a day T as a
SIGNAL when atm_iv[T] is >=2 standard deviations above its own trailing-20-day mean (and
days_to_expiry[T] >= 3). Outcome = |close[T+1]-close[T]|/close[T] on ohlcv_candles (1d),
divided by that stock's own trailing-20-day average |daily return|. Control = the same ratio
on a random non-signal day for the same stock/window.

Exploratory only -- 83 days of history, no train/hold-out split (see the hypothesis doc's
data-availability correction). Rule is frozen before this script is run; not re-tuned after
seeing output.

Usage:
    python scripts/research/test_g_iv_divergence.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import psycopg2
from dotenv import dotenv_values

IV_Z_THRESHOLD = 2.0
MIN_DTE = 3
MIN_PRIOR_SNAPSHOTS = 30
LOOKBACK = 20
RANDOM_SEED = 42


def load_iv_history(conn) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT underlying AS symbol, snapshot_date, atm_iv, days_to_expiry "
        "FROM options_iv_history ORDER BY underlying, snapshot_date",
        conn,
    )
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"])
    return df


def load_daily_closes(conn, symbols: list[str]) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT symbol, ts, close FROM ohlcv_candles WHERE timeframe='1d' AND symbol = ANY(%s) "
        "ORDER BY symbol, ts",
        conn,
        params=(symbols,),
    )
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None).dt.normalize()
    return df


def main() -> None:
    conn = psycopg2.connect(dotenv_values(ROOT / ".env")["DB_URL"])
    conn.set_session(readonly=True)
    try:
        iv = load_iv_history(conn)
        symbols = sorted(iv["symbol"].unique())
        px = load_daily_closes(conn, symbols)
    finally:
        conn.close()

    print(
        f"options_iv_history: {len(iv)} rows, {len(symbols)} symbols, "
        f"{iv['snapshot_date'].min().date()} -> {iv['snapshot_date'].max().date()}"
    )

    rng = random.Random(RANDOM_SEED)
    signal_ratios: list[float] = []
    control_ratios: list[float] = []
    n_symbols_used = 0

    for sym, g in iv.groupby("symbol"):
        g = g.sort_values("snapshot_date").reset_index(drop=True)
        if len(g) < MIN_PRIOR_SNAPSHOTS + 1:
            continue
        p = px[px["symbol"] == sym].sort_values("ts").reset_index(drop=True)
        if len(p) < LOOKBACK + 2:
            continue
        p = p.set_index("ts")
        ret = p["close"].pct_change()
        abs_ret = ret.abs()
        trailing_avg_abs_ret = abs_ret.rolling(LOOKBACK).mean()

        atm = g["atm_iv"].astype(float)
        roll_mean = atm.rolling(LOOKBACK).mean()
        roll_std = atm.rolling(LOOKBACK).std()
        iv_z = (atm - roll_mean) / roll_std.replace(0, np.nan)

        used_this_symbol = False
        candidate_days: list[int] = []
        for i in range(MIN_PRIOR_SNAPSHOTS, len(g)):
            day = g.loc[i, "snapshot_date"]
            dte = g.loc[i, "days_to_expiry"]
            z = iv_z.iloc[i]
            if pd.isna(z) or dte is None or dte < MIN_DTE:
                continue
            candidate_days.append(i)
            next_day = day + pd.Timedelta(days=1)
            # find the next trading day's close vs this day's close in px
            if day not in p.index:
                continue
            future = p.index[p.index > day]
            if len(future) == 0:
                continue
            t1 = future[0]
            if t1 not in ret.index or pd.isna(trailing_avg_abs_ret.get(day, np.nan)):
                continue
            baseline = trailing_avg_abs_ret.get(day)
            if not baseline or baseline <= 0:
                continue
            ratio = abs(ret.loc[t1]) / baseline
            if pd.isna(ratio):
                continue
            if z >= IV_Z_THRESHOLD:
                signal_ratios.append(ratio)
                used_this_symbol = True

        # control: one random non-signal candidate day per symbol with >=1 signal, same count
        # of draws as signals found for that symbol (so control sample size tracks signal size)
        n_sig_this_symbol = sum(
            1
            for i in candidate_days
            if not pd.isna(iv_z.iloc[i]) and iv_z.iloc[i] >= IV_Z_THRESHOLD
        )
        non_signal_days = [
            i for i in candidate_days if pd.isna(iv_z.iloc[i]) or iv_z.iloc[i] < IV_Z_THRESHOLD
        ]
        if n_sig_this_symbol > 0 and non_signal_days:
            picks = rng.sample(non_signal_days, k=min(n_sig_this_symbol, len(non_signal_days)))
            for i in picks:
                day = g.loc[i, "snapshot_date"]
                if day not in p.index:
                    continue
                future = p.index[p.index > day]
                if len(future) == 0:
                    continue
                t1 = future[0]
                baseline = trailing_avg_abs_ret.get(day)
                if not baseline or baseline <= 0:
                    continue
                ratio = abs(ret.loc[t1]) / baseline if t1 in ret.index else None
                if ratio is not None and not pd.isna(ratio):
                    control_ratios.append(ratio)

        if used_this_symbol:
            n_symbols_used += 1

    print(f"\nSymbols with >=1 signal event: {n_symbols_used}/{len(symbols)}")
    print(f"Signal events: n={len(signal_ratios)}")
    print(f"Control events: n={len(control_ratios)}")

    if signal_ratios:
        sr = np.array(signal_ratios)
        print(
            f"\nSignal-day ratio:  mean={sr.mean():.3f}  median={np.median(sr):.3f}  "
            f"std={sr.std():.3f}"
        )
    if control_ratios:
        cr = np.array(control_ratios)
        print(
            f"Control-day ratio: mean={cr.mean():.3f}  median={np.median(cr):.3f}  "
            f"std={cr.std():.3f}"
        )
    if signal_ratios and control_ratios:
        lift = sr.mean() / cr.mean() if cr.mean() > 0 else float("nan")
        print(f"\nLift (signal mean / control mean): {lift:.3f}x")
        # Welch's t-test (no scipy dependency assumed; implement directly)
        n1, n2 = len(sr), len(cr)
        v1, v2 = sr.var(ddof=1), cr.var(ddof=1)
        se = (v1 / n1 + v2 / n2) ** 0.5
        t_stat = (sr.mean() - cr.mean()) / se if se > 0 else float("nan")
        print(f"Welch t-stat (signal vs control): {t_stat:.2f} (|t|>=2 ~ p<0.05 rough guide)")

        if len(sr) < 40:
            print(
                "\nVERDICT: n<40 signal events -- per pre-registered bar, do not trust either way."
            )
        elif lift >= 1.3:
            print(
                "\nVERDICT: lift >= 1.3x with n>=40 -- worth continuing to Phase 1 (OI/depth collection)."
            )
        elif lift < 1.1:
            print("\nVERDICT: lift < 1.1x -- treat as null, do not build further on IV alone.")
        else:
            print("\nVERDICT: lift in 1.1-1.3x band -- report only, re-test at the 6-month mark.")
    else:
        print("\nVERDICT: insufficient data to evaluate.")


if __name__ == "__main__":
    main()
