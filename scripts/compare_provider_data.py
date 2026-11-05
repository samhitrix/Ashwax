"""Compare candles from two market-data providers (parity check before switching order).

Usage: python scripts/compare_provider_data.py --a kite --b angel_one            --symbols RELIANCE,INFY --interval day --days 30
``--a`` is the baseline (put the incumbent first): close diff = |a-b|/a * 100 and
volume_ratio = b/a, both on common timestamps only.
Both providers must be listed in config/global.json market_data.providers and have
credentials in .env. Read-only: fetches via the configured adapters, writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).parent.parent
_IST = "Asia/Kolkata"


def _naive_ist(dates: pd.Series) -> pd.Series:
    """Normalise timestamps to tz-naive IST so aware and naive frames align.

    Naive values are assumed to already be IST (the exchange clock); aware values
    are converted to IST first, so the same instant compares equal either way.
    """
    s = pd.to_datetime(dates)
    if s.dt.tz is not None:
        s = s.dt.tz_convert(_IST).dt.tz_localize(None)
    return s


def compare_frames(a: pd.DataFrame, b: pd.DataFrame) -> dict[str, float]:
    """Compare two OHLCV frames on their common timestamps.

    Differences are relative to ``a`` (the baseline). Returns NaN metrics, never
    raises, when a frame is empty, lacks ``date``, or the frames share no timestamps.
    """
    nan = float("nan")
    if "date" not in a.columns or "date" not in b.columns or a.empty or b.empty:
        return {
            "rows_a": float(len(a)),
            "rows_b": float(len(b)),
            "common": 0.0,
            "max_close_diff_pct": nan,
            "mean_close_diff_pct": nan,
            "volume_ratio_mean": nan,
        }
    a = a.assign(date=_naive_ist(a["date"])).set_index("date")
    b = b.assign(date=_naive_ist(b["date"])).set_index("date")
    common = a.index.intersection(b.index)
    out = {"rows_a": float(len(a)), "rows_b": float(len(b)), "common": float(len(common))}
    if len(common) == 0:
        return {
            **out,
            "max_close_diff_pct": float("nan"),
            "mean_close_diff_pct": float("nan"),
            "volume_ratio_mean": float("nan"),
        }
    ca, cb = a.loc[common, "close"].astype(float), b.loc[common, "close"].astype(float)
    diff = (ca - cb).abs() / ca * 100.0
    va, vb = a.loc[common, "volume"].astype(float), b.loc[common, "volume"].astype(float)
    ratio = (vb / va.where(va != 0)).dropna()
    return {
        **out,
        "max_close_diff_pct": float(diff.max()),
        "mean_close_diff_pct": float(diff.mean()),
        "volume_ratio_mean": float(ratio.mean()) if len(ratio) else float("nan"),
    }


async def _fetch(router: Any, name: str, symbol: str, interval: str, days: int) -> pd.DataFrame:
    adapter = router.provider(name)
    if adapter is None:
        raise SystemExit(
            f"provider {name!r} unavailable - it must be listed in config/global.json "
            f"market_data.providers AND have credentials in .env (eligible now: {router.names})"
        )
    df = await adapter.get_ohlcv(f"NSE:{symbol}", interval, days)
    df.columns = [c.lower() for c in df.columns]
    return df


async def _main(args: argparse.Namespace) -> None:
    from src.brokers.factory import build_market_data_router
    from src.market_data.router import NoMarketDataProviderError

    try:
        router = build_market_data_router()  # once: Angel reloads its instrument master per adapter
    except NoMarketDataProviderError as exc:
        raise SystemExit(str(exc)) from exc
    for sym in args.symbols.split(","):
        try:
            a = await _fetch(router, args.a, sym, args.interval, args.days)
            b = await _fetch(router, args.b, sym, args.interval, args.days)
            print(sym, compare_frames(a, b))
        except Exception as exc:  # one bad symbol must not abort the soak run
            print(sym, "ERROR", exc)


if __name__ == "__main__":
    from dotenv import load_dotenv

    sys.path.insert(0, str(ROOT))  # so `src.*` imports work when run as a script
    load_dotenv(ROOT / ".env")  # provider eligibility is read from os.environ
    p = argparse.ArgumentParser()
    p.add_argument(
        "--a", required=True, help="baseline provider: diffs are |a-b|/a, volume ratio b/a"
    )
    p.add_argument("--b", required=True, help="provider compared against the baseline")
    p.add_argument("--symbols", required=True)
    p.add_argument("--interval", default="day")
    p.add_argument("--days", type=int, default=30)
    asyncio.run(_main(p.parse_args()))
