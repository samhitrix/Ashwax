"""Hypothesis A — pullback_in_uptrend. Rules/bars frozen in docs/hypotheses/A_pullback_in_uptrend.md.

Usage: python scripts/research/test_a_pullback.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
from datetime import date

import numpy as np
import pandas as pd
from common import Window, atr, by_window, fmt, load_candles, rsi, score
from src.strategies.uss.compiler import BacktestSignal
from src.strategies.uss.schema import Direction

COST_PCT = 0.25
WINDOWS = [
    Window("train", date(2021, 1, 1), date(2025, 6, 30)),
    Window("holdout1", date(2025, 7, 1), date(2026, 7, 7)),
    Window("fresh", date(2026, 7, 8), date(2100, 1, 1)),
]


def prepare(g: pd.DataFrame) -> pd.DataFrame:
    g = g.reset_index(drop=True)
    g["sma200"] = g["close"].rolling(200).mean()
    g["sma5"] = g["close"].rolling(5).mean()
    g["rsi2"] = rsi(g["close"], 2)
    g["atr14"] = atr(g, 14)
    g["adv"] = (g["close"] * g["volume"]).rolling(20).mean()
    g["eligible"] = (
        g["sma200"].notna() & g["atr14"].notna() & g["close"].between(50, 5000) & (g["adv"] >= 1e8)
    )
    return g


def simulate(g: pd.DataFrame, entry_mask: np.ndarray) -> list[BacktestSignal]:
    """Signal at close t → entry open t+1; stop 2.5*ATR(t); exit next open after
    close>SMA5 or 10 days held; stop fills at stop or the gap open if worse."""
    o, h, low, c = (g[k].to_numpy() for k in ("open", "high", "low", "close"))
    sma5, a = g["sma5"].to_numpy(), g["atr14"].to_numpy()
    out: list[BacktestSignal] = []
    n = len(g)
    t = 0
    while t < n - 1:
        if not entry_mask[t]:
            t += 1
            continue
        e_i = t + 1
        entry = o[e_i]
        stop = entry - 2.5 * a[t]
        if stop <= 0 or entry <= stop:
            t += 1
            continue
        exit_px, exit_i, reason = None, None, ""
        j = e_i
        while j < n:
            if o[j] <= stop and j > e_i:
                exit_px, exit_i, reason = o[j], j, "SL gap"
                break
            if low[j] <= stop:
                exit_px, exit_i, reason = min(stop, o[j]), j, "SL"
                break
            held = j - e_i + 1
            if (c[j] > sma5[j] or held >= 10) and j + 1 < n:
                exit_px, exit_i = o[j + 1], j + 1
                reason = "MA Close" if c[j] > sma5[j] else "Time Stop"
                break
            j += 1
        if exit_px is None:  # still open at end of data — not scored
            break
        out.append(
            BacktestSignal(
                bar_index=e_i,
                date=g["ts"].iloc[e_i],
                entry_price=entry,
                sl=stop,
                target=float("nan"),
                exit_price=exit_px,
                exit_bar=exit_i,
                pnl_r=(exit_px - entry) / (entry - stop),
                direction=Direction.LONG,
                exit_reason=reason,
            )
        )
        t = exit_i  # one position per symbol at a time
    return out


def main() -> None:
    df = load_candles("1d")
    df = df[~df["symbol"].str.contains(" ")]  # drop indices
    print(f"symbols={df['symbol'].nunique()}  bars={len(df)}  cost={COST_PCT}%")
    groups = {s: prepare(g) for s, g in df.groupby("symbol")}

    a_sigs: list[BacktestSignal] = []
    n_elig = n_sig = 0
    for g in groups.values():
        mask = (g["eligible"] & (g["close"] > g["sma200"]) & (g["rsi2"] < 10)).to_numpy()
        n_elig += int(g["eligible"].sum())
        n_sig += int(mask.sum())
        a_sigs += simulate(g, mask)

    # Control: random entries on the same eligible bars at A's signal rate, same exits.
    rng = np.random.default_rng(20261005)
    p = n_sig / max(n_elig, 1)
    c_sigs: list[BacktestSignal] = []
    for g in groups.values():
        mask = (g["eligible"].to_numpy()) & (rng.random(len(g)) < p)
        c_sigs += simulate(g, mask)

    print(f"signal rate on eligible bars = {p:.4f}\n")
    for label, sigs in (("A pullback", a_sigs), ("RANDOM control", c_sigs)):
        print(f"== {label} ==")
        for w, s in by_window(sigs, WINDOWS).items():
            print(fmt(w, score(w, s, COST_PCT)))
        print()

    ex = pd.Series([s.exit_reason for s in a_sigs]).value_counts()
    print("A exit reasons:", ex.to_dict())


if __name__ == "__main__":
    main()
