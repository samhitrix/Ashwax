# ruff: noqa: S311, B007, B023, F841  -- one-off research script (bootstrap sampling, loop closures used immediately); logic left untouched so recorded results stay reproducible
"""
Hypothesis G re-run on the larger reconstructed panel (data/research/fo_iv_oi_panel.parquet,
built by build_fo_historical_panel.py from NSE's public bhavcopy archive) instead of the
83-day live options_iv_history table the original test_g_iv_divergence.py used.

SAME frozen rule as the original pre-registration (docs/hypotheses/G_options_iv_divergence.md):
iv_z = (atm_iv[T] - mean(atm_iv[T-20:T])) / std(atm_iv[T-20:T]) >= 2.0, dte[T] >= 3, outcome =
|ret[T+1]| / trailing-20d avg |ret|, vs a random-day control. Only the DATA SOURCE changed (more
history), not the rule -- this is not a re-tune of a result already seen on the smaller sample.

Adds a train/hold-out split, which the 83-day version didn't have enough data for: first 70% of
the panel's date range = train, last 30% = fresh hold-out, pre-registered split point computed
from the data's actual date range (not cherry-picked).

Usage:
    python scripts/research/test_g_iv_divergence_v2.py
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

PANEL_PATH = ROOT / "data" / "research" / "fo_iv_oi_panel.parquet"
IV_Z_THRESHOLD = 2.0
MIN_DTE = 3
MIN_PRIOR = 30
LOOKBACK = 20
RANDOM_SEED = 42


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    se = (a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)) ** 0.5
    return (a.mean() - b.mean()) / se if se > 0 else float("nan")


def run(panel: pd.DataFrame, label: str, rng: random.Random) -> None:
    signal_ratios: list[float] = []
    control_ratios: list[float] = []

    for sym, g in panel.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_PRIOR + 2:
            continue
        g = g.set_index(pd.DatetimeIndex(pd.to_datetime(g["date"])))
        close = g["underlying_close"]
        ret = close.pct_change()
        abs_ret = ret.abs()
        trailing_avg_abs_ret = abs_ret.rolling(LOOKBACK).mean()

        atm = g["atm_iv"]
        iv_z = (atm - atm.rolling(LOOKBACK).mean()) / atm.rolling(LOOKBACK).std().replace(0, np.nan)

        candidate_idx = []
        for i in range(MIN_PRIOR, len(g)):
            z = iv_z.iloc[i]
            dte = g["dte"].iloc[i]
            if pd.isna(z) or dte < MIN_DTE:
                continue
            candidate_idx.append(i)

        sig_idx = [i for i in candidate_idx if iv_z.iloc[i] >= IV_Z_THRESHOLD]
        non_sig_idx = [i for i in candidate_idx if iv_z.iloc[i] < IV_Z_THRESHOLD]

        def ratio_at(i: int) -> float | None:
            day = g.index[i]
            if i + 1 >= len(g):
                return None
            next_day = g.index[i + 1]
            if (next_day - day).days > 5:  # guard against data gaps
                return None
            baseline = trailing_avg_abs_ret.iloc[i]
            if not baseline or pd.isna(baseline) or baseline <= 0:
                return None
            r1 = ret.iloc[i + 1]
            if pd.isna(r1):
                return None
            return abs(r1) / baseline

        for i in sig_idx:
            r = ratio_at(i)
            if r is not None:
                signal_ratios.append(r)

        if sig_idx and non_sig_idx:
            picks = rng.sample(non_sig_idx, k=min(len(sig_idx), len(non_sig_idx)))
            for i in picks:
                r = ratio_at(i)
                if r is not None:
                    control_ratios.append(r)

    sr = np.array(signal_ratios)
    cr = np.array(control_ratios)
    print(f"\n--- {label} ---")
    print(f"Signal events: n={len(sr)}   Control events: n={len(cr)}")
    if len(sr) == 0 or len(cr) == 0:
        print("Insufficient data.")
        return
    lift = sr.mean() / cr.mean() if cr.mean() > 0 else float("nan")
    t_stat = welch_t(sr, cr)
    print(
        f"Signal mean={sr.mean():.3f} median={np.median(sr):.3f} | "
        f"Control mean={cr.mean():.3f} median={np.median(cr):.3f}"
    )
    print(f"Lift={lift:.3f}x   Welch t={t_stat:.2f}")
    if len(sr) < 40:
        print("VERDICT: n<40 -- do not trust either way.")
    elif lift >= 1.3:
        print("VERDICT: lift>=1.3x, n>=40 -- PASS (per pre-registered bar).")
    elif lift < 1.1:
        print("VERDICT: lift<1.1x -- NULL.")
    else:
        print("VERDICT: 1.1-1.3x band -- report only.")


def main() -> None:
    if not PANEL_PATH.exists():
        raise SystemExit(f"{PANEL_PATH} not found -- run build_fo_historical_panel.py first")
    panel = pd.read_parquet(PANEL_PATH)
    panel["date"] = pd.to_datetime(panel["date"])
    dmin, dmax = panel["date"].min(), panel["date"].max()
    print(
        f"Panel: {len(panel)} rows, {panel['symbol'].nunique()} symbols, "
        f"{dmin.date()} -> {dmax.date()}"
    )

    rng = random.Random(RANDOM_SEED)
    run(panel, "FULL WINDOW (all available history)", rng)

    split = dmin + (dmax - dmin) * 0.7
    train = panel[panel["date"] <= split]
    holdout = panel[panel["date"] > split]
    print(
        f"\nPre-registered 70/30 split at {split.date()} "
        f"(train {dmin.date()}->{split.date()}, holdout {split.date()}->{dmax.date()})"
    )
    rng = random.Random(RANDOM_SEED)
    run(train, "TRAIN (first 70% of date range)", rng)
    rng = random.Random(RANDOM_SEED)
    run(holdout, "HOLD-OUT (last 30% of date range, never used to pick the rule)", rng)


if __name__ == "__main__":
    main()
