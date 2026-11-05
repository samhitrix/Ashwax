"""F-multi — expiry-day structure choice (condor / butterfly / trend spread). Rules frozen in
docs/hypotheses/F_nifty_expiry_day_condor.md (section F-multi). MODEL, not fills.

Usage: python scripts/research/test_f_multi.py
"""

from __future__ import annotations

import math

# ruff: noqa: E402
# mypy: ignore-errors
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from test_f_expiry_day import (
    _N,
    LOT,
    NIFTY_TOKEN,
    STEP,
    VIX_TOKEN,
    WINDOWS,
    bs,
    expiry_days,
    fetch_5m,
    years_left,
)

R = 0.065
T10 = datetime.strptime("10:00", "%H:%M").time()
T915 = datetime.strptime("09:15", "%H:%M").time()
T1445 = datetime.strptime("14:45", "%H:%M").time()


def strike_at_delta(s, t, vol, call, delta):
    z = _N.inv_cdf(delta if call else 1 - delta)
    k = s * math.exp((R + vol * vol / 2) * t - z * vol * math.sqrt(t))
    return (math.ceil(k / STEP) if call else math.floor(k / STEP)) * STEP


def legs_for(kind, s, t, vol, trend):
    """List of (strike, is_call, sign) where sign=-1 short, +1 long; and wing width."""
    if kind == "F3" or kind == "F4":
        if trend >= 0.003:
            k = strike_at_delta(s, t, vol, False, 0.20)
            return [(k, False, -1), (k - 100, False, +1)], 100.0, "bull_put"
        if trend <= -0.003:
            k = strike_at_delta(s, t, vol, True, 0.20)
            return [(k, True, -1), (k + 100, True, +1)], 100.0, "bear_call"
        kind = "F1" if kind == "F3" else "F2"
    if kind == "F1":
        kc, kp = strike_at_delta(s, t, vol, True, 0.15), strike_at_delta(s, t, vol, False, 0.15)
        return (
            [(kc, True, -1), (kc + 100, True, +1), (kp, False, -1), (kp - 100, False, +1)],
            100.0,
            "condor",
        )
    atm = round(s / STEP) * STEP
    return (
        [(atm, True, -1), (atm + 150, True, +1), (atm, False, -1), (atm - 150, False, +1)],
        150.0,
        "butterfly",
    )


def value(legs, s, t, vol):  # value of the SHORT position = what it costs to buy back
    return -sum(sign * bs(s, k, t, vol, call) for k, call, sign in legs)


def run(nifty, vix, kind, iv_mult):
    days = sorted(set(nifty.index.date) & set(vix.index.date))
    rows = []
    for d in expiry_days(days):
        nd, vd = nifty[nifty.index.date == d], vix[vix.index.date == d]
        ent, opn = nd[nd.index.time == T10], nd[nd.index.time == T915]
        if ent.empty or opn.empty or vd.empty:
            continue
        t0, s0 = ent.index[0], float(ent["open"].iloc[0])
        trend = s0 / float(opn["open"].iloc[0]) - 1
        va = vd[vd.index <= t0]
        vol0 = float(va["close"].iloc[-1] if not va.empty else vd["open"].iloc[0]) / 100 * iv_mult
        tl0 = years_left(t0)
        legs, wing, shape = legs_for(kind, s0, tl0, vol0, trend)
        credit = value(legs, s0, tl0, vol0)
        if credit <= 0.5:
            continue
        cost = 3.0 if len(legs) == 4 else 2.0
        exit_v, why = None, ""
        for ts, bar in nd[nd.index > t0].iterrows():
            if ts.time() > T1445:
                break
            vv = vd[vd.index <= ts]
            vol = float(vv["close"].iloc[-1]) / 100 * iv_mult if not vv.empty else vol0
            v = value(legs, float(bar["close"]), years_left(ts + timedelta(minutes=5)), vol)
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
                shape=shape,
                credit=credit,
                pnl=credit - exit_v - cost,
                max_loss=wing - credit,
                why=why,
            )
        )
    return pd.DataFrame(rows)


def main() -> None:
    nifty, vix = fetch_5m(NIFTY_TOKEN, "nifty50"), fetch_5m(VIX_TOKEN, "indiavix")
    for x in (nifty, vix):
        x.index = x.index.tz_localize(None) if x.index.tz is not None else x.index
    for kind in ("F1", "F2", "F3", "F4"):
        for mult in (1.0, 0.8):
            df = run(nifty, vix, kind, mult)
            for name, a, b in WINDOWS:
                w = df[(df.day >= a) & (df.day <= b)]
                if w.empty:
                    continue
                print(
                    f"{kind} IVx{mult} {name:<8} n={len(w):>3} E={w.pnl.mean():+6.2f}pts "
                    f"= {w.pnl.mean() / w.max_loss.mean():+6.1%} of max loss  win={np.mean(w.pnl > 0):5.1%} "
                    f"worst={w.pnl.min():+7.1f}  sum={w.pnl.sum() * LOT:+,.0f}Rs/lot  "
                    f"shapes={w['shape'].value_counts().to_dict()}"
                )
        print()


if __name__ == "__main__":
    main()
