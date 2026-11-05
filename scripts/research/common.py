"""Shared helpers for pre-registered hypothesis tests (docs/hypotheses/).

Read-only on the DB. Scoring reuses BacktestEngine's own metric maths so the
numbers are comparable with every earlier audit (docs/strategy_expectancy_audit_2026-07-07.md).
"""

from __future__ import annotations

# ruff: noqa: E402
# mypy: ignore-errors
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
import psycopg2
from dotenv import dotenv_values
from src.strategies.backtest.engine import BacktestEngine
from src.strategies.uss.compiler import BacktestSignal

IST = "Asia/Kolkata"


def load_candles(timeframe: str, symbols: list[str] | None = None) -> pd.DataFrame:
    """Load OHLCV for one timeframe (read-only session). ts is converted to IST."""
    conn = psycopg2.connect(dotenv_values(ROOT / ".env")["DB_URL"])
    conn.set_session(readonly=True)
    try:
        sql = (
            "SELECT symbol, ts, open, high, low, close, volume FROM ohlcv_candles "
            "WHERE timeframe = %s"
        )
        params: list = [timeframe]
        if symbols is not None:
            sql += " AND symbol = ANY(%s)"
            params.append(symbols)
        df = pd.read_sql(sql + " ORDER BY symbol, ts", conn, params=params)
    finally:
        conn.close()
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = wilder(d.clip(lower=0), n)
    dn = wilder(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return wilder(tr, n)


@dataclass
class Window:
    name: str
    start: date
    end: date


def score(name: str, sigs: list[BacktestSignal], cost_pct: float) -> dict:
    """Engine metrics for a signal list (n<20 → engine reports zeros; we still show n)."""
    r = BacktestEngine(cost_pct=cost_pct)._score(name, sigs)
    return {
        "n": r.total_trades,
        "wr": r.win_rate,
        "pf": r.profit_factor,
        "e": r.expectancy_r,
        "dd": r.max_drawdown_r,
    }


def by_window(sigs: list[BacktestSignal], windows: list[Window]) -> dict[str, list]:
    out: dict[str, list] = {w.name: [] for w in windows}
    for s in sigs:
        d = pd.Timestamp(s.date).date()
        for w in windows:
            if w.start <= d <= w.end:
                out[w.name].append(s)
    return out


def fmt(name: str, m: dict) -> str:
    return (
        f"{name:<16} n={m['n']:>5}  WR={m['wr']:6.1%}  PF={m['pf']:5.2f}  "
        f"E={m['e']:+.3f}R  maxDD={m['dd']:6.1f}R"
    )
