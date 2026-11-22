"""Hypothesis B — nifty_weekly_condor_simple. Rules/bars frozen in
docs/hypotheses/B_nifty_weekly_condor_simple.md.

No historical option prices exist, so:
  1. VRP test (real data): how often NIFTY's move to expiry stays inside +/-1x the
     VIX-implied move. ~68% if implied vol is fairly priced.
  2. MODELED P&L (not real fills): Black-Scholes with VIX as a flat IV (no skew),
     daily closes only, fixed costs of 3 NIFTY points per condor round trip.
India VIX daily is fetched read-only from Kite (needs a valid KITE_ACCESS_TOKEN).

Usage: python scripts/research/test_b_condor.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import math
from datetime import date

import numpy as np
import pandas as pd
from common import ROOT, load_candles
from dotenv import dotenv_values

R_FREE = 0.065
WING = 200.0
STEP = 50.0
TARGET_DELTA = 0.15
COST_PTS = 3.0  # 8 orders brokerage (~2.1 pts/lot) + 2-tick slippage x 4 legs x 2 sides + STT
TUESDAY_FROM = date(2025, 9, 1)  # NSE moved NIFTY weekly expiry Thu -> Tue in Sep 2025
WINDOWS = [
    ("train", date(2021, 1, 1), date(2024, 12, 31)),
    ("holdout", date(2025, 1, 1), date(2100, 1, 1)),
]


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(s: float, k: float, t: float, vol: float, call: bool) -> float:
    if t <= 0:
        return max(s - k, 0.0) if call else max(k - s, 0.0)
    d1 = (math.log(s / k) + (R_FREE + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if call:
        return s * ncdf(d1) - k * math.exp(-R_FREE * t) * ncdf(d2)
    return k * math.exp(-R_FREE * t) * ncdf(-d2) - s * ncdf(-d1)


def strike_for_delta(s: float, t: float, vol: float, call: bool) -> float:
    # call delta = N(d1) = 0.15 ; put |delta| = N(-d1) = 0.15
    from statistics import NormalDist

    z = NormalDist().inv_cdf(TARGET_DELTA if call else 1 - TARGET_DELTA)
    k = s * math.exp((R_FREE + vol * vol / 2) * t - z * vol * math.sqrt(t))
    return (math.ceil(k / STEP) if call else math.floor(k / STEP)) * STEP


def condor_value(s: float, t: float, vol: float, kc: float, kp: float) -> float:
    return (
        bs(s, kc, t, vol, True)
        - bs(s, kc + WING, t, vol, True)
        + bs(s, kp, t, vol, False)
        - bs(s, kp - WING, t, vol, False)
    )


def fetch_vix() -> pd.Series:
    from kiteconnect import KiteConnect

    e = dotenv_values(ROOT / ".env")
    k = KiteConnect(api_key=e["KITE_API_KEY"])
    k.set_access_token(e["KITE_ACCESS_TOKEN"])
    rows = []
    for y in range(2020, date.today().year + 1):
        rows += k.historical_data(264969, date(y, 1, 1), min(date(y, 12, 31), date.today()), "day")
    df = pd.DataFrame(rows)
    return pd.Series(df["close"].values, index=pd.to_datetime(df["date"]).dt.date)


def expiries(days: list[date]) -> list[date]:
    """Last trading day on/before the nominal weekly expiry weekday, per ISO week."""
    out, by_week = [], {}
    for d in days:
        by_week.setdefault(d.isocalendar()[:2], []).append(d)
    for wk in sorted(by_week):
        ds = by_week[wk]
        wd = 1 if ds[0] >= TUESDAY_FROM else 3  # Tue / Thu
        cand = [d for d in ds if d.weekday() <= wd]
        if cand:
            out.append(max(cand))
    return out


def main() -> None:
    nifty = load_candles("1d", ["NIFTY 50"])
    nifty["d"] = nifty["ts"].dt.date
    nifty = nifty.drop_duplicates("d").set_index("d")
    vix = fetch_vix()
    days = [d for d in nifty.index if d in vix.index]
    pos = {d: i for i, d in enumerate(days)}
    exps = expiries(days)

    rows = []
    for prev_exp, exp in zip(exps, exps[1:], strict=False):
        i0 = pos[prev_exp] + 1  # entry: first trading day of the new cycle
        if i0 >= pos[exp] - 1:  # need >= 2 trading days to expiry
            continue
        entry_d = days[i0]
        s0 = float(nifty.at[entry_d, "open"])
        vol0 = float(vix[days[i0 - 1]]) / 100  # prior close VIX, known at 10:00
        t0 = (exp - entry_d).days / 365 + 0.25 / 365
        s_exp = float(nifty.at[exp, "close"])
        implied = s0 * vol0 * math.sqrt(t0)
        inside = abs(s_exp - s0) <= implied

        kc = strike_for_delta(s0, t0, vol0, True)
        kp = strike_for_delta(s0, t0, vol0, False)
        credit = condor_value(s0, t0, vol0, kc, kp)
        exit_val, reason = None, ""
        for j in range(i0, pos[exp]):  # daily closes through the day before expiry
            d = days[j]
            t = (exp - d).days / 365
            v = condor_value(float(nifty.at[d, "close"]), t, float(vix[d]) / 100, kc, kp)
            if v <= 0.5 * credit:
                exit_val, reason = v, "TP50"
                break
            if v - credit >= 2 * credit:
                exit_val, reason = v, "SL2x"
                break
        if exit_val is None:
            exit_val, reason = v, "ExpiryEve"
        pnl = credit - exit_val - COST_PTS
        rows.append(
            dict(
                entry=entry_d,
                inside=inside,
                credit=credit,
                pnl=pnl,
                max_loss=WING - credit,
                reason=reason,
            )
        )

    df = pd.DataFrame(rows)
    print(
        f"weeks={len(df)}  first={df.entry.min()}  last={df.entry.max()}  "
        f"(MODEL: BS, flat VIX IV, daily closes, cost {COST_PTS} pts)\n"
    )
    for name, a, b in WINDOWS:
        w = df[(df.entry >= a) & (df.entry <= b)]
        e_pct = w.pnl.mean() / w.max_loss.mean()
        print(
            f"{name:<8} weeks={len(w):>4}  inside={w.inside.mean():6.1%}  "
            f"avg_credit={w.credit.mean():6.1f}  E/trade={w.pnl.mean():+6.2f} pts "
            f"= {e_pct:+.1%} of max loss  win={np.mean(w.pnl > 0):5.1%}  "
            f"worst={w.pnl.min():+7.1f}  exits={w.reason.value_counts().to_dict()}"
        )


if __name__ == "__main__":
    main()
