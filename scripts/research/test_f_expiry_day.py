"""Hypothesis F — NIFTY expiry-day (0-DTE) iron condor. Rules/bars frozen in
docs/hypotheses/F_nifty_expiry_day_condor.md. MODEL (Black-Scholes, India VIX as flat IV),
5-minute NIFTY + VIX fetched read-only from Kite (cached in the scratch dir below).

Usage: python scripts/research/test_f_expiry_day.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import math
import os
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
from common import ROOT
from dotenv import dotenv_values

CACHE = Path(os.environ.get("RESEARCH_CACHE", ROOT / "data" / "research_cache"))
NIFTY_TOKEN, VIX_TOKEN = 256265, 264969
SHORT_DELTA, WING, STEP, LOT, COST_PTS, R = 0.15, 100.0, 50.0, 65, 3.0, 0.065
TUESDAY_FROM = date(2025, 9, 1)
WINDOWS = [
    ("train", date(2021, 1, 1), date(2024, 12, 31)),
    ("holdout", date(2025, 1, 1), date(2100, 1, 1)),
]
_N = NormalDist()


def fetch_5m(token: int, name: str) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{name}_5m.pkl"
    if f.exists() and (_time.time() - f.stat().st_mtime) < 86400:
        return pd.read_pickle(f)  # noqa: S301 -- our own cache file, not untrusted input
    from kiteconnect import KiteConnect

    e = dotenv_values(ROOT / ".env")
    k = KiteConnect(api_key=e["KITE_API_KEY"])
    k.set_access_token(e["KITE_ACCESS_TOKEN"])
    rows, start = [], date(2021, 1, 1)
    while start <= date.today():
        end = min(start + timedelta(days=95), date.today())
        rows += k.historical_data(token, start, end, "5minute")
        start = end + timedelta(days=1)
        _time.sleep(0.4)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df = df[~df.index.duplicated()]
    df.to_pickle(f)
    return df


def bs(s, k, t, vol, call):  # t in years
    if t <= 0:
        return max(s - k, 0.0) if call else max(k - s, 0.0)
    d1 = (math.log(s / k) + (R + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return s * _N.cdf(d1) - k * math.exp(-R * t) * _N.cdf(d2)
    return k * math.exp(-R * t) * _N.cdf(-d2) - s * _N.cdf(-d1)


def strike(s, t, vol, call):
    z = _N.inv_cdf(SHORT_DELTA if call else 1 - SHORT_DELTA)
    k = s * math.exp((R + vol * vol / 2) * t - z * vol * math.sqrt(t))
    return (math.ceil(k / STEP) if call else math.floor(k / STEP)) * STEP


def condor(s, t, vol, kc, kp):
    return (
        bs(s, kc, t, vol, True)
        - bs(s, kc + WING, t, vol, True)
        + bs(s, kp, t, vol, False)
        - bs(s, kp - WING, t, vol, False)
    )


def years_left(ts) -> float:
    mins = (15 * 60 + 30) - (ts.hour * 60 + ts.minute)
    return max(mins, 0) / 375.0 / 252.0


def expiry_days(days: list[date]) -> list[date]:
    by_week: dict = {}
    for d in days:
        by_week.setdefault(d.isocalendar()[:2], []).append(d)
    out = []
    for wk in sorted(by_week):
        ds = by_week[wk]
        wd = 1 if ds[0] >= TUESDAY_FROM else 3
        cand = [d for d in ds if d.weekday() <= wd]
        if cand:
            out.append(max(cand))
    return out


def run(nifty: pd.DataFrame, vix: pd.DataFrame, iv_mult: float) -> pd.DataFrame:
    days = sorted(set(nifty.index.date) & set(vix.index.date))
    rows = []
    for d in expiry_days(days):
        nd = nifty[nifty.index.date == d]
        vd = vix[vix.index.date == d]
        ent = nd[nd.index.time == datetime.strptime("10:00", "%H:%M").time()]
        if ent.empty or vd.empty:
            continue
        t0 = ent.index[0]
        s0 = float(ent["open"].iloc[0])
        v_at = vd[vd.index <= t0]
        vol0 = (
            float(v_at["close"].iloc[-1] if not v_at.empty else vd["open"].iloc[0]) / 100 * iv_mult
        )
        tl0 = years_left(t0)
        kc, kp = strike(s0, tl0, vol0, True), strike(s0, tl0, vol0, False)
        credit = condor(s0, tl0, vol0, kc, kp)
        if credit <= 0.5:
            continue
        exit_v, why = None, ""
        for ts, bar in nd[(nd.index > t0)].iterrows():
            if ts.time() > datetime.strptime("14:45", "%H:%M").time():
                break
            vv = vd[vd.index <= ts]
            vol = float(vv["close"].iloc[-1]) / 100 * iv_mult if not vv.empty else vol0
            # bar is labelled by start; its close is 5 min later
            t_close = years_left(ts + timedelta(minutes=5))
            v = condor(float(bar["close"]), t_close, vol, kc, kp)
            exit_v = v
            if v <= 0.5 * credit:
                why = "TP50"
                break
            if v - credit >= 2 * credit:
                why = "SL2x"
                break
            why = "Time1445"
        if exit_v is None:
            continue
        rows.append(
            dict(
                day=d,
                credit=credit,
                pnl=credit - exit_v - COST_PTS,
                max_loss=WING - credit,
                why=why,
                move=abs(float(nd["close"].iloc[-1]) / s0 - 1),
            )
        )
    return pd.DataFrame(rows)


def main() -> None:
    nifty = fetch_5m(NIFTY_TOKEN, "nifty50")
    vix = fetch_5m(VIX_TOKEN, "indiavix")
    for x in (nifty, vix):
        x.index = x.index.tz_localize(None) if x.index.tz is not None else x.index
    print(
        f"NIFTY 5m {nifty.index[0].date()}..{nifty.index[-1].date()} ({len(nifty)} bars); "
        f"VIX 5m {vix.index[0].date()}..{vix.index[-1].date()} ({len(vix)} bars)"
    )
    print(
        f"MODEL: BS, VIX as flat IV, short {SHORT_DELTA} delta, wings {WING:g}, cost {COST_PTS} pts, "
        f"entry 10:00, exit TP50/SL2x/14:45\n"
    )
    for mult in (1.0, 0.8, 1.2):
        df = run(nifty, vix, mult)
        print(f"== IV x {mult} ==")
        for name, a, b in WINDOWS:
            w = df[(df.day >= a) & (df.day <= b)]
            if w.empty:
                print(f"  {name}: no trades")
                continue
            print(
                f"  {name:<8} n={len(w):>3}  credit={w.credit.mean():5.1f}  "
                f"E={w.pnl.mean():+6.2f} pts = {w.pnl.mean() / w.max_loss.mean():+6.1%} of max loss  "
                f"win={np.mean(w.pnl > 0):5.1%}  worst={w.pnl.min():+7.1f}  "
                f"sum={w.pnl.sum() * LOT:+,.0f} Rs/lot  exits={w.why.value_counts().to_dict()}"
            )
        print()


if __name__ == "__main__":
    main()
