"""Hypothesis B2 — lower-gamma NIFTY condor. Rules/bars frozen in
docs/hypotheses/B2_nifty_condor_lower_gamma.md. Reuses test_b_condor's model
(Black-Scholes, India VIX as flat IV, daily closes, fixed cost). MODEL, not fills.

Usage: python scripts/research/test_b2_condor.py
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import math
from datetime import date

import numpy as np
import pandas as pd
from common import load_candles
from test_b_condor import (
    COST_PTS,
    R_FREE,
    WING,
    condor_value,
    expiries,
    fetch_vix,
    strike_for_delta,
)

LOT = 65
WINDOWS = [
    ("train", date(2021, 1, 1), date(2024, 12, 31)),
    ("holdout", date(2025, 1, 1), date(2100, 1, 1)),
]
VARIANTS = {"B (nearest)": 0, "B2-14 (2nd weekly)": 1, "B2-28 (4th weekly)": 3}
GAMMA_BUDGET = 80_000.0


def bs_gamma(s: float, k: float, t: float, vol: float) -> float:
    d1 = (math.log(s / k) + (R_FREE + vol * vol / 2) * t) / (vol * math.sqrt(t))
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (s * vol * math.sqrt(t))


def condor_gamma_rs(s: float, t: float, vol: float, kc: float, kp: float) -> float:
    net = (
        -bs_gamma(s, kc, t, vol)
        - bs_gamma(s, kp, t, vol)
        + bs_gamma(s, kc + WING, t, vol)
        + bs_gamma(s, kp - WING, t, vol)
    )
    return abs(net) * LOT * s * s / 100.0


def run(nifty, vix, days, pos, exps, ahead: int) -> pd.DataFrame:
    rows, busy_until = [], None
    for k in range(len(exps) - 1 - ahead):
        i0 = pos[exps[k]] + 1  # first trading day of the cycle after expiry k
        if i0 >= len(days):
            break
        entry_d = days[i0]
        if busy_until is not None and entry_d <= busy_until:
            continue
        exp = exps[k + 1 + ahead]
        last = (
            max(j for j in range(i0, pos[exp]) if (exp - days[j]).days >= 4)
            if any((exp - days[j]).days >= 4 for j in range(i0, pos[exp]))
            else None
        )
        if last is None:
            continue
        s0 = float(nifty.at[entry_d, "open"])
        vol0 = float(vix[days[i0 - 1]]) / 100
        t0 = (exp - entry_d).days / 365 + 0.25 / 365
        kc, kp = strike_for_delta(s0, t0, vol0, True), strike_for_delta(s0, t0, vol0, False)
        credit = condor_value(s0, t0, vol0, kc, kp)
        gamma = condor_gamma_rs(s0, t0, vol0, kc, kp)
        exit_val, reason, x = None, "", last
        for j in range(i0, last + 1):
            d = days[j]
            v = condor_value(
                float(nifty.at[d, "close"]), (exp - d).days / 365, float(vix[d]) / 100, kc, kp
            )
            if v <= 0.5 * credit:
                exit_val, reason, x = v, "TP50", j
                break
            if v - credit >= 2 * credit:
                exit_val, reason, x = v, "SL2x", j
                break
        if exit_val is None:
            exit_val, reason = v, "Guardian(DTE<=3)"
        busy_until = days[x]
        rows.append(
            dict(
                entry=entry_d,
                dte=(exp - entry_d).days,
                credit=credit,
                gamma=gamma,
                pnl=credit - exit_val - COST_PTS,
                max_loss=WING - credit,
                reason=reason,
                held=x - i0 + 1,
            )
        )
    return pd.DataFrame(rows)


def main() -> None:
    nifty = load_candles("1d", ["NIFTY 50"])
    nifty["d"] = nifty["ts"].dt.date
    nifty = nifty.drop_duplicates("d").set_index("d")
    vix = fetch_vix()
    days = [d for d in nifty.index if d in vix.index]
    pos = {d: i for i, d in enumerate(days)}
    exps = expiries(days)
    print(
        f"MODEL (BS, flat VIX IV, daily closes, cost {COST_PTS} pts, lot {LOT}); gamma budget Rs {GAMMA_BUDGET:,.0f}\n"
    )
    for name, ahead in VARIANTS.items():
        df = run(nifty, vix, days, pos, exps, ahead)
        print(f"== {name} ==")
        for wname, a, b in WINDOWS:
            w = df[(df.entry >= a) & (df.entry <= b)]
            if w.empty:
                print(f"  {wname}: no trades")
                continue
            print(
                f"  {wname:<8} n={len(w):>3}  dte~{w.dte.median():.0f}  E={w.pnl.mean():+6.2f} pts "
                f"= {w.pnl.mean() / w.max_loss.mean():+6.1%} of max loss  win={np.mean(w.pnl > 0):5.1%}  "
                f"worst={w.pnl.min():+7.1f}  credit={w.credit.mean():5.1f}  "
                f"gamma/lot median=Rs{w.gamma.median():,.0f} p90=Rs{w.gamma.quantile(0.9):,.0f}  "
                f"held~{w.held.median():.0f}d  exits={w.reason.value_counts().to_dict()}"
            )
        print()


if __name__ == "__main__":
    main()
