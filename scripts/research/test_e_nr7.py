"""Hypothesis E — nr7_range_expansion. Rules/bars frozen in
docs/hypotheses/E_nr7_range_expansion.md. 5m bars are labelled by START time (IST).

Usage: python scripts/research/test_e_nr7.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
from datetime import date, time

import numpy as np
import pandas as pd
from common import Window, by_window, fmt, load_candles, score
from src.strategies.uss.compiler import BacktestSignal
from src.strategies.uss.schema import Direction

COST_PCT = 0.10
WINDOWS = [
    Window("train", date(2026, 3, 2), date(2026, 7, 31)),
    Window("holdout", date(2026, 8, 1), date(2100, 1, 1)),
]
LAST_TRIGGER = time(14, 25)  # bar starting 14:25 closes 14:30
EXIT_BAR = time(15, 10)


def setups(daily: pd.DataFrame, nr: int) -> pd.DataFrame:
    """One row per (symbol, trade day D) built only from data known at D-1 close."""
    out = []
    for s, g in daily.groupby("symbol"):
        g = g.reset_index(drop=True)
        c = g["close"]
        rng = g["high"] - g["low"]
        ok = (
            (rng == rng.rolling(nr).min())
            & (rng >= 0.01 * c)
            & c.between(50, 5000)
            & ((c * g["volume"]).rolling(20).mean() >= 1e8)
        )
        sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
        side = np.where((c > sma50) & (c > sma200), 1, np.where((c < sma50) & (c < sma200), -1, 0))
        for i in np.flatnonzero(ok.to_numpy() & (side != 0))[:]:
            if i + 1 < len(g):
                out.append(
                    (s, g["d"].iloc[i + 1], int(side[i]), g["high"].iloc[i], g["low"].iloc[i])
                )
    return pd.DataFrame(out, columns=["symbol", "d", "side", "ph", "pl"])


def trade(bars: pd.DataFrame, side: int, ph: float, pl: float, earliest: time, rand_t=None):
    """Return (trigger_time, signal) or None."""
    t = bars["t"].to_numpy()
    o, h, low, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    if len(t) == 0 or t[0] != time(9, 15) or EXIT_BAR not in set(t):
        return None
    level, stop = (ph, pl) if side == 1 else (pl, ph)
    if (side == 1 and o[0] > ph) or (side == -1 and o[0] < pl):
        return None  # gapped through: no chase
    trig = None
    for i in range(len(t) - 1):
        if t[i] < earliest or t[i] > LAST_TRIGGER:
            continue
        if rand_t is not None:
            if t[i] == rand_t:
                trig = i
                break
        elif (side == 1 and c[i] > level) or (side == -1 and c[i] < level):
            trig = i
            break
    if trig is None:
        return None
    e = trig + 1
    entry = o[e]
    if side * (entry - stop) <= 0:
        return None
    exit_px, why = None, "EOD"
    for j in range(e, len(t)):
        if t[j] > EXIT_BAR:
            break
        if side == 1 and low[j] <= stop:
            exit_px, why = min(stop, o[j]), "SL"
            break
        if side == -1 and h[j] >= stop:
            exit_px, why = max(stop, o[j]), "SL"
            break
        if t[j] == EXIT_BAR:
            exit_px = c[j]
            break
    if exit_px is None:
        return None
    sig = BacktestSignal(
        bar_index=e,
        date=pd.Timestamp(bars["d"].iloc[0]),
        entry_price=entry,
        sl=stop,
        target=float("nan"),
        exit_price=exit_px,
        pnl_r=side * (exit_px - entry) / abs(entry - stop),
        direction=Direction.LONG if side == 1 else Direction.SHORT,
        exit_reason=why,
    )
    return t[trig], sig


def run(st: pd.DataFrame, idx: dict, bars: pd.DataFrame, earliest: time, rng=None) -> list:
    sigs = []
    slots = [time(h, m) for h in range(9, 15) for m in range(0, 60, 5)]
    for d, day in st.groupby("d"):
        cands = []
        for r in day.itertuples():
            pos = idx.get((r.symbol, d))
            if pos is None:
                continue
            rt = None
            if rng is not None:
                ok = [s for s in slots if earliest <= s <= LAST_TRIGGER]
                rt = ok[rng.integers(len(ok))]
            res = trade(bars.iloc[pos], r.side, r.ph, r.pl, earliest, rt)
            if res:
                cands.append(res)
        cands.sort(key=lambda x: x[0])
        sigs += [s for _, s in cands[:5]]
    return sigs


def main() -> None:
    daily = load_candles("1d")
    daily = daily[~daily["symbol"].str.contains(" ")]
    daily["d"] = daily["ts"].dt.date
    bars = load_candles("5m")
    bars = bars[~bars["symbol"].str.contains(" ")].reset_index(drop=True)
    bars["d"], bars["t"] = bars["ts"].dt.date, bars["ts"].dt.time
    idx = bars.groupby(["symbol", "d"]).indices
    first5m = bars["d"].min()
    print(
        f"5m symbols={bars['symbol'].nunique()}  days {first5m} to {bars['d'].max()}  cost={COST_PCT}%\n"
    )

    st7 = setups(daily, 7)
    st7 = st7[st7["d"] >= first5m]
    e_sigs = run(st7, idx, bars, time(9, 30))
    c_sigs = run(st7, idx, bars, time(9, 30), rng=np.random.default_rng(20261005))
    for label, sigs in (("E NR7 (FROZEN)", e_sigs), ("RANDOM-time control", c_sigs)):
        print(f"== {label} ==  setups={len(st7)}")
        for w, s in by_window(sigs, WINDOWS).items():
            print(fmt(w, score(w, s, COST_PCT)))
            print(fmt(w + " pre-cost", score(w, s, 0.0)))
        print()
    print("E exit reasons:", pd.Series([s.exit_reason for s in e_sigs]).value_counts().to_dict())
    risk = pd.Series([abs(s.entry_price - s.sl) / s.entry_price for s in e_sigs])
    print(
        f"E stop distance: median {risk.median():.2%}  -> cost = {COST_PCT / 100 / risk.median():.2f}R"
    )

    print("\n== Robustness (train E, cost incl.) ==")
    for nr in (4, 7, 10):
        st = setups(daily, nr)
        st = st[st["d"] >= first5m]
        row = []
        for ear in (time(9, 30), time(10, 0)):
            m = score("t", by_window(run(st, idx, bars, ear), WINDOWS[:1])["train"], COST_PCT)
            row.append(f"from {ear:%H:%M}: E={m['e']:+.3f} n={m['n']}")
        print(f"NR{nr:<3} " + "  |  ".join(row))


if __name__ == "__main__":
    main()
