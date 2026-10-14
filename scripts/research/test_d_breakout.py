"""Hypothesis D — breadth_gated_breakout. Rules/bars frozen in
docs/hypotheses/D_breadth_gated_breakout.md.

Usage: python scripts/research/test_d_breakout.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
from datetime import date

import numpy as np
import pandas as pd
from common import Window, atr, by_window, fmt, load_candles, score
from src.strategies.uss.compiler import BacktestSignal
from src.strategies.uss.schema import Direction

COST_PCT = 0.25
BREADTH, CHANNEL = 0.50, 50
WINDOWS = [
    Window("train", date(2021, 1, 1), date(2025, 6, 30)),
    Window("holdout1", date(2025, 7, 1), date(2026, 7, 7)),
    Window("fresh", date(2026, 7, 8), date(2100, 1, 1)),
]


def prepare(g: pd.DataFrame) -> pd.DataFrame:
    g = g.reset_index(drop=True)
    c = g["close"]
    g["d"] = g["ts"].dt.date
    g["sma50"], g["sma200"] = c.rolling(50).mean(), c.rolling(200).mean()
    g["atr14"] = atr(g, 14)
    g["adv"] = (c * g["volume"]).rolling(20).mean()
    g["vavg50"] = g["volume"].rolling(50).mean()
    g["ret126"] = c / c.shift(126) - 1
    for n in (20, 50, 100):
        g[f"hh{n}"] = c.shift(1).rolling(n).max()
    g["eligible"] = (
        g["sma200"].notna() & g["atr14"].notna() & c.between(50, 5000) & (g["adv"] >= 1e8)
    )
    return g


def signal_mask(g: pd.DataFrame, channel: int, breadth: pd.Series | None, thr: float) -> np.ndarray:
    m = (
        g["eligible"]
        & (g["close"] > g[f"hh{channel}"])
        & (g["sma50"] > g["sma200"])
        & (g["volume"] >= 1.5 * g["vavg50"])
    )
    if breadth is not None:
        m &= g["d"].map(breadth).fillna(0) >= thr
    return m.to_numpy()


def simulate(g: pd.DataFrame, mask: np.ndarray, sym: str) -> list[tuple]:
    o, low, c = (g[k].to_numpy() for k in ("open", "low", "close"))
    a, r126, d = g["atr14"].to_numpy(), g["ret126"].to_numpy(), g["d"].to_numpy()
    out, n, t = [], len(g), 0
    while t < n - 1:
        if not mask[t]:
            t += 1
            continue
        e_i = t + 1
        entry = o[e_i]
        stop = entry - 2 * a[t]
        if stop <= 0 or entry <= stop:
            t += 1
            continue
        hc, trail, exit_px, x_i, why = -np.inf, -np.inf, None, None, ""
        for j in range(e_i, n):
            if j > e_i and o[j] <= stop:
                exit_px, x_i, why = o[j], j, "SL gap"
                break
            if low[j] <= stop:
                exit_px, x_i, why = min(stop, o[j]), j, "SL"
                break
            hc = max(hc, c[j])
            trail = max(trail, hc - 3 * a[j])
            if c[j] < trail and j + 1 < n:
                exit_px, x_i, why = o[j + 1], j + 1, "Trail"
                break
        if exit_px is None:
            # Still open: mark to last close. Dropping open trades would keep only the
            # fast stop-outs in the latest window (right-censoring bias against trend trades).
            exit_px, x_i, why = c[n - 1], n - 1, "Open MTM"
        sig = BacktestSignal(
            bar_index=e_i,
            date=pd.Timestamp(d[e_i]),
            entry_price=entry,
            sl=stop,
            target=float("nan"),
            exit_price=exit_px,
            exit_bar=x_i,
            pnl_r=(exit_px - entry) / (entry - stop),
            direction=Direction.LONG,
            exit_reason=why,
        )
        out.append((sig, d[e_i], d[x_i], r126[t], sym))
        t = x_i
    return out


def run(groups, mask_fn) -> list[tuple]:
    trades = []
    for s, g in groups.items():
        trades += simulate(g, mask_fn(g), s)
    return trades


def cost_r(sig) -> float:
    return min(COST_PCT / 100 / ((sig.entry_price - sig.sl) / sig.entry_price), 1.0)


def portfolio(trades: list[tuple], all_days: list, w: Window, cap: int = 10) -> str:
    tr = sorted(
        [x for x in trades if w.start <= x[1] <= w.end],
        key=lambda x: (x[1], -np.nan_to_num(x[3], nan=-9)),
    )
    open_, taken = [], []
    for x in tr:
        open_ = [e for e in open_ if e > x[1]]
        if len(open_) < cap:
            open_.append(x[2])
            taken.append(x)
    if not taken:
        return f"{w.name:<9} portfolio: no trades"
    taken.sort(key=lambda x: x[2])
    r = np.array([x[0].pnl_r - cost_r(x[0]) for x in taken])
    eq = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq]))[1:] - eq))
    days = [d for d in all_days if w.start <= d <= w.end]
    busy = set()
    for x in taken:
        busy.update(d for d in days if x[1] <= d < x[2])
    months = max(len(days) / 21, 1)
    return (
        f"{w.name:<9} portfolio(cap {cap}): trades={len(taken):>4} ({len(taken) / months:4.1f}/mo)  "
        f"sumR={eq[-1]:+7.1f}  E={r.mean():+.3f}R  maxDD={dd:5.1f}R  flat_days={1 - len(busy) / max(len(days), 1):5.1%}"
    )


def main() -> None:
    df = load_candles("1d")
    df = df[~df["symbol"].str.contains(" ")]
    groups = {s: prepare(g) for s, g in df.groupby("symbol")}
    allg = pd.concat(groups.values())
    v = allg[allg["sma200"].notna()]
    breadth = (v["close"] > v["sma200"]).groupby(v["d"]).mean()
    breadth = breadth[v.groupby("d").size() >= 100]  # need a real cross-section
    all_days = sorted(breadth.index)
    print(
        f"symbols={len(groups)}  breadth days={len(breadth)} ({all_days[0]} to {all_days[-1]})  cost={COST_PCT}%"
    )
    for w in WINDOWS:
        b = breadth[[w.start <= d <= w.end for d in breadth.index]]
        print(f"  breadth {w.name}: mean={b.mean():.1%}  days>=50%={(b >= 0.5).mean():.1%}")
    print()

    d_tr = run(groups, lambda g: signal_mask(g, CHANNEL, breadth, BREADTH))
    u_tr = run(groups, lambda g: signal_mask(g, CHANNEL, None, 0))

    n_sig = sum(int(signal_mask(g, CHANNEL, breadth, BREADTH).sum()) for g in groups.values())
    gate_elig = {
        s: (g["eligible"] & (g["d"].map(breadth).fillna(0) >= BREADTH)).to_numpy()
        for s, g in groups.items()
    }
    p = n_sig / max(sum(int(m.sum()) for m in gate_elig.values()), 1)
    rng = np.random.default_rng(20261005)
    c_tr = run(groups, lambda g: gate_elig[g["symbol"].iloc[0]] & (rng.random(len(g)) < p))

    for label, tr in (("D gated (FROZEN)", d_tr), ("Ungated", u_tr), ("RANDOM control", c_tr)):
        print(f"== {label} ==")
        for w, s in by_window([x[0] for x in tr], WINDOWS).items():
            print(fmt(w, score(w, s, COST_PCT)))
        print()
    print("== D portfolio sim ==")
    for w in WINDOWS:
        print(portfolio(d_tr, all_days, w))
    print("\nD exit reasons:", pd.Series([x[0].exit_reason for x in d_tr]).value_counts().to_dict())

    print("\n== Robustness grid (train E, cost incl.) ==")
    for thr in (0.40, 0.50, 0.60):
        row = []
        for ch in (20, 50, 100):
            tr = run(groups, lambda g, ch=ch, thr=thr: signal_mask(g, ch, breadth, thr))
            m = score("t", by_window([x[0] for x in tr], WINDOWS[:1])["train"], COST_PCT)
            row.append(f"ch{ch}: E={m['e']:+.3f} n={m['n']}")
        print(f"breadth>={thr:.0%}  " + "  |  ".join(row))


if __name__ == "__main__":
    main()
