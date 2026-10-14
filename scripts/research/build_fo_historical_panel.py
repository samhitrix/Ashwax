"""
One-time, offline, research-only historical panel builder for Hypotheses G/H
(docs/hypotheses/). Downloads NSE's own public historical F&O bhavcopy archive
(nsearchives.nseindia.com -- no auth, no API key, confirmed reachable 2026-10-08 back to
at least Oct 2024) and reconstructs, per stock-option underlying per trading day:

  - atm_iv: mean of CE/PE implied vol at the strike nearest the day's underlying close,
    at the nearest expiry with >=14 calendar days to expiry -- this exactly mirrors
    OptionChain.atm_iv()/atm() (src/options/nfo_chain.py) and the live snapshot job's
    min_days_to_expiry=14 default (src/orchestrator.py ~12024), so historical and live
    options_iv_history rows are methodologically comparable.
  - near_month_oi / near_month_oi_chg: total OpnIntrst / ChngInOpnIntrst (CE+PE) at that
    same nearest expiry, summed across all strikes -- taken directly from the bhavcopy,
    no reconstruction needed (unlike IV, OI is reported as-is by NSE).
  - underlying_close: UndrlygPric, as reported in the bhavcopy (NSE's own settlement
    reference price for that underlying that day).

Implied vol uses the SAME Black-Scholes solver already live in the system
(src/options/greeks.py implied_vol/bs_price) -- no new math, no new dependency.

This is read-only against NSE's public archive and writes only to a local parquet file
under data/research/ -- nothing touches the live DB or any trading path.

Usage:
    python scripts/research/build_fo_historical_panel.py --days 365
    python scripts/research/build_fo_historical_panel.py --days 30   # quick validation run
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import argparse
import io
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
import requests
from src.options.greeks import DEFAULT_RISK_FREE_RATE, days_to_years, implied_vol

CACHE_DIR = ROOT / "data" / "research" / "fo_bhavcopy_cache"
OUT_PATH = ROOT / "data" / "research" / "fo_iv_oi_panel.parquet"
URL_TMPL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
MIN_DAYS_TO_EXPIRY = 14  # matches the live snapshot job's default


def fetch_day(d: date) -> pd.DataFrame | None:
    """Download + parse one day's F&O bhavcopy. None on holiday/weekend/failure."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_csv = CACHE_DIR / f"{d:%Y%m%d}.csv"
    if cache_csv.exists():
        return pd.read_csv(cache_csv)
    url = URL_TMPL.format(d=f"{d:%Y%m%d}")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or len(resp.content) < 1000:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        name = zf.namelist()[0]
        with zf.open(name) as f:
            df = pd.read_csv(f)
    except (zipfile.BadZipFile, IndexError, pd.errors.ParserError):
        return None
    df.to_csv(cache_csv, index=False)
    return df


def reconstruct_day(df: pd.DataFrame, d: date) -> list[dict]:
    """One row per stock-option underlying for this day: atm_iv, OI, underlying close."""
    rows: list[dict] = []
    df = df[df["FinInstrmTp"] == "STO"].copy()
    if df.empty:
        return rows
    df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
    trad_dt = d
    df["dte"] = df["XpryDt"].apply(lambda x: (x - trad_dt).days)
    eligible = df[df["dte"] >= MIN_DAYS_TO_EXPIRY]
    if eligible.empty:
        return rows

    for sym, g in eligible.groupby("TckrSymb"):
        nearest_expiry = g.loc[g["dte"].idxmin(), "XpryDt"]
        exp_rows = g[g["XpryDt"] == nearest_expiry]
        spot = float(exp_rows["UndrlygPric"].iloc[0])
        if spot <= 0:
            continue
        t = days_to_years(float(exp_rows["dte"].iloc[0]))
        strikes = exp_rows["StrkPric"].astype(float)
        atm_strike = strikes.iloc[(strikes - spot).abs().argsort().iloc[0]]
        atm_rows = exp_rows[exp_rows["StrkPric"].astype(float) == atm_strike]

        ivs: list[float] = []
        for _, row in atm_rows.iterrows():
            price = float(row["SttlmPric"])
            kind = str(row["OptnTp"])
            if price <= 0 or kind not in ("CE", "PE"):
                continue
            iv = implied_vol(price, spot, float(atm_strike), t, DEFAULT_RISK_FREE_RATE, kind)
            if iv is not None:
                ivs.append(iv)
        if not ivs:
            continue
        atm_iv = sum(ivs) / len(ivs)
        near_month_oi = float(exp_rows["OpnIntrst"].sum())
        near_month_oi_chg = float(exp_rows["ChngInOpnIntrst"].sum())

        rows.append(
            {
                "symbol": sym,
                "date": d,
                "atm_iv": atm_iv,
                "near_month_oi": near_month_oi,
                "near_month_oi_chg": near_month_oi_chg,
                "underlying_close": spot,
                "dte": int(exp_rows["dte"].iloc[0]),
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365, help="calendar days to look back")
    ap.add_argument("--sleep", type=float, default=0.3, help="delay between requests (s)")
    args = ap.parse_args()

    end = date.today()
    start = end - timedelta(days=args.days)
    all_rows: list[dict] = []
    fetched, skipped = 0, 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri only; weekends/holidays fall through fetch_day->None
            df = fetch_day(d)
            if df is not None:
                day_rows = reconstruct_day(df, d)
                all_rows.extend(day_rows)
                fetched += 1
                if fetched % 20 == 0:
                    print(
                        f"  ...{fetched} trading days processed, latest {d}, "
                        f"{len(all_rows)} panel rows so far"
                    )
                time.sleep(args.sleep)
            else:
                skipped += 1
        d += timedelta(days=1)

    panel = pd.DataFrame(all_rows)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(OUT_PATH, index=False)
    print(f"\nDone. trading_days_fetched={fetched} skipped(weekend/holiday/fail)={skipped}")
    print(
        f"Panel: {len(panel)} rows, {panel['symbol'].nunique() if len(panel) else 0} symbols, "
        f"{panel['date'].min() if len(panel) else '-'} -> {panel['date'].max() if len(panel) else '-'}"
    )
    print(f"Written to {OUT_PATH}")


if __name__ == "__main__":
    main()
