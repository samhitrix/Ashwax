"""Hypothesis C — intraday_momentum_last_hour. Rules/bars frozen in
docs/hypotheses/C_intraday_momentum_last_hour.md.

5m bars are labelled by START time (IST), so the bar starting 10:10 closes at 10:15.
Usage: python scripts/research/test_c_last_hour.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
from datetime import date, time

import pandas as pd
from common import ROOT, Window, atr, by_window, fmt, load_candles, score
from src.strategies.uss.compiler import BacktestSignal
from src.strategies.uss.schema import Direction

COST_PCT = 0.10
THRESH = 0.004
WINDOWS = [
    Window("train", date(2026, 3, 2), date(2026, 7, 31)),
    Window("holdout", date(2026, 8, 1), date(2100, 1, 1)),
]


def nifty50() -> list[str]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("sd", ROOT / "scripts/strategy_diagnosis.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.NIFTY50)


def main() -> None:
    syms = nifty50()
    idx = load_candles("5m", ["NIFTY 50"])
    bars = load_candles("5m", syms)
    daily = load_candles("1d", syms)

    # 50-DMA as of the PREVIOUS day's close (no look-ahead).
    daily["d"] = daily["ts"].dt.date
    daily["sma50_prev"] = daily.groupby("symbol")["close"].transform(
        lambda s: s.rolling(50).mean().shift(1)
    )
    sma50 = daily.set_index(["symbol", "d"])["sma50_prev"]

    bars["d"] = bars["ts"].dt.date
    bars["t"] = bars["ts"].dt.time
    bars["atr14"] = bars.groupby("symbol", group_keys=False).apply(lambda g: atr(g, 14))
    idx["d"] = idx["ts"].dt.date
    idx["t"] = idx["ts"].dt.time

    sigs: list[BacktestSignal] = []
    days = {"long": 0, "short": 0, "none": 0}
    for d, di in idx.groupby("d"):
        first = di[di["t"] == time(9, 15)]
        hour = di[di["t"] == time(10, 10)]
        if first.empty or hour.empty:
            continue
        r1h = hour["close"].iloc[0] / first["open"].iloc[0] - 1
        side = "long" if r1h >= THRESH else "short" if r1h <= -THRESH else "none"
        days[side] += 1
        if side == "none":
            continue
        day = bars[bars["d"] == d]
        cands = []
        for s, g in day.groupby("symbol"):
            g = g.set_index("t")
            if not {time(9, 15), time(14, 10), time(14, 15), time(15, 10)} <= set(g.index):
                continue
            ret = g.at[time(14, 10), "close"] / g.at[time(9, 15), "open"] - 1
            cands.append((ret, s, g))
        cands.sort(key=lambda x: x[0], reverse=(side == "long"))
        for _ret, s, g in cands[:5]:
            px_rank = g.at[time(14, 10), "close"]
            if side == "short":
                m = sma50.get((s, d))
                if m is None or pd.isna(m) or px_rank >= m:
                    continue  # counter-trend short rule
            a = g.at[time(14, 10), "atr14"]
            entry = g.at[time(14, 15), "open"]
            sgn = 1 if side == "long" else -1
            stop = entry - sgn * a
            exit_px, reason = None, "EOD"
            for t in [x for x in g.index if time(14, 15) <= x <= time(15, 10)]:
                o, hi, lo = g.at[t, "open"], g.at[t, "high"], g.at[t, "low"]
                if sgn == 1 and lo <= stop:
                    exit_px, reason = min(stop, o), "SL"
                    break
                if sgn == -1 and hi >= stop:
                    exit_px, reason = max(stop, o), "SL"
                    break
            if exit_px is None:
                exit_px = g.at[time(15, 10), "close"]
            sigs.append(
                BacktestSignal(
                    bar_index=0,
                    date=pd.Timestamp(d),
                    entry_price=entry,
                    sl=stop,
                    target=float("nan"),
                    exit_price=exit_px,
                    pnl_r=sgn * (exit_px - entry) / a,
                    direction=Direction.LONG if sgn == 1 else Direction.SHORT,
                    exit_reason=reason,
                )
            )

    print(f"index days: {days}  trades={len(sigs)}  cost={COST_PCT}%\n")
    for w, s in by_window(sigs, WINDOWS).items():
        print(fmt(w, score(w, s, COST_PCT)))
        print(fmt(w + " pre-cost", score(w, s, 0.0)))
    print("\nexit reasons:", pd.Series([s.exit_reason for s in sigs]).value_counts().to_dict())


if __name__ == "__main__":
    main()
