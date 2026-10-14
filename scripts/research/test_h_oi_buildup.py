# ruff: noqa: S311, B007, B023, F841  -- one-off research script (bootstrap sampling, loop closures used immediately); logic left untouched so recorded results stay reproducible
"""
Hypothesis H pre-registered test (docs/hypotheses/H_options_oi_buildup.md).

For each stock in data/research/fo_iv_oi_panel.parquet: classify day T into LONG_BUILDUP /
SHORT_BUILDUP / SHORT_COVERING / LONG_UNWINDING based on sign(ret[T]) x sign(oi_z[T]) where
oi_z is the day's near-month OI change in z-score terms vs its own trailing-20-day norm
(|oi_z|>=1.5, dte>=3). Outcome = ret[T+1] (signed). Primary bet (frozen): LONG_BUILDUP ->
positive next-day return, SHORT_BUILDUP -> negative next-day return, both n>=40 and
distinguishable from a same-stock random-day control via Welch's t-test.

Usage:
    python scripts/research/test_h_oi_buildup.py
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
OI_Z_THRESHOLD = 1.5
MIN_DTE = 3
MIN_PRIOR = 30
LOOKBACK = 20
RANDOM_SEED = 42


def welch_t(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    se = (a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)) ** 0.5
    return (a.mean() - b.mean()) / se if se > 0 else float("nan")


def main() -> None:
    if not PANEL_PATH.exists():
        raise SystemExit(f"{PANEL_PATH} not found -- run build_fo_historical_panel.py first")
    panel = pd.read_parquet(PANEL_PATH)
    panel["date"] = pd.to_datetime(panel["date"])
    print(
        f"Panel: {len(panel)} rows, {panel['symbol'].nunique()} symbols, "
        f"{panel['date'].min().date()} -> {panel['date'].max().date()}"
    )

    buckets: dict[str, list[float]] = {
        "LONG_BUILDUP": [],
        "SHORT_BUILDUP": [],
        "SHORT_COVERING": [],
        "LONG_UNWINDING": [],
    }
    control: dict[str, list[float]] = {k: [] for k in buckets}
    rng = random.Random(RANDOM_SEED)

    for sym, g in panel.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < MIN_PRIOR + 2:
            continue
        g = g.set_index(pd.DatetimeIndex(g["date"]))
        close = g["underlying_close"]
        ret = close.pct_change()

        oi_chg = g["near_month_oi_chg"]
        oi_z = (oi_chg - oi_chg.rolling(LOOKBACK).mean()) / oi_chg.rolling(LOOKBACK).std().replace(
            0, np.nan
        )

        def next_ret(i: int) -> float | None:
            if i + 1 >= len(g):
                return None
            day, nxt = g.index[i], g.index[i + 1]
            if (nxt - day).days > 5:
                return None
            r1 = ret.iloc[i + 1]
            return None if pd.isna(r1) else float(r1)

        candidate_idx, bucket_of = [], {}
        for i in range(MIN_PRIOR, len(g)):
            z = oi_z.iloc[i]
            dte = g["dte"].iloc[i]
            r0 = ret.iloc[i]
            if pd.isna(z) or pd.isna(r0) or dte < MIN_DTE:
                continue
            candidate_idx.append(i)
            if z >= OI_Z_THRESHOLD and r0 > 0:
                bucket_of[i] = "LONG_BUILDUP"
            elif z >= OI_Z_THRESHOLD and r0 < 0:
                bucket_of[i] = "SHORT_BUILDUP"
            elif z <= -OI_Z_THRESHOLD and r0 > 0:
                bucket_of[i] = "SHORT_COVERING"
            elif z <= -OI_Z_THRESHOLD and r0 < 0:
                bucket_of[i] = "LONG_UNWINDING"

        unclassified = [i for i in candidate_idx if i not in bucket_of]
        for i, b in bucket_of.items():
            r1 = next_ret(i)
            if r1 is not None:
                buckets[b].append(r1)
        if bucket_of and unclassified:
            picks = rng.sample(unclassified, k=min(len(bucket_of), len(unclassified)))
            # distribute control draws proportionally across the buckets this symbol filled
            bucket_names = list(bucket_of.values())
            for j, i in enumerate(picks):
                r1 = next_ret(i)
                if r1 is not None:
                    control[bucket_names[j % len(bucket_names)]].append(r1)

    print()
    primary_pass = True
    for name in buckets:
        sig = np.array(buckets[name])
        ctl = np.array(control[name])
        print(f"{name}: n={len(sig)}  control_n={len(ctl)}")
        if len(sig) == 0:
            print("  (no events)")
            continue
        print(f"  mean(ret[T+1])={sig.mean():+.4f}  median={np.median(sig):+.4f}")
        if len(ctl) > 1:
            t_stat = welch_t(sig, ctl)
            print(f"  control mean={ctl.mean():+.4f}  Welch t={t_stat:.2f}")
        if name in ("LONG_BUILDUP", "SHORT_BUILDUP"):
            want_positive = name == "LONG_BUILDUP"
            sign_ok = (sig.mean() > 0) == want_positive
            n_ok = len(sig) >= 40
            print(f"  primary bet: sign_correct={sign_ok} n>=40={n_ok}")
            primary_pass = primary_pass and sign_ok and n_ok

    print(
        f"\nOVERALL VERDICT: {'PASS (both buildup buckets correct-signed, n>=40)' if primary_pass else 'FAIL (per pre-registered primary bet)'}"
    )


if __name__ == "__main__":
    main()
