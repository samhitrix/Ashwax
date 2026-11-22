"""One-time IV-history bootstrap sourced from Kite historical India VIX candles,
instead of a manually downloaded NSE CSV (see src/options/bootstrap_iv_history.py
for the CSV path — this reuses its seed_from_series()).

Run once: python -m scripts.bootstrap_iv_from_broker --days 365
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from src.brokers.factory import create_broker
from src.db.pool import init_pool
from src.db.repositories.options import OptionsRepository
from src.options.bootstrap_iv_history import seed_from_series

load_dotenv(Path(__file__).parent.parent / ".env")


async def _fetch_vix_series(days: int) -> list[tuple[date, float]]:
    broker = create_broker("kite")
    ohlcv = await broker.get_ohlcv("NSE:INDIA VIX", "day", days)
    if ohlcv is None or ohlcv.empty:
        raise RuntimeError("Kite returned no historical India VIX candles")
    rows = [(row["date"].date(), float(row["close"])) for _, row in ohlcv.iterrows()]
    rows.sort(key=lambda r: r[0])
    return rows


async def _run(days: int, indices: list[str]) -> None:
    vix_rows = await _fetch_vix_series(days)
    print(
        f"Fetched {len(vix_rows)} India VIX daily closes "
        f"({vix_rows[0][0]} .. {vix_rows[-1][0]})"
    )

    pool = await init_pool()
    repo = OptionsRepository(pool)
    count = await seed_from_series(repo, vix_rows, indices)
    print(f"Inserted {count} rows into options_vol_intel_history.")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=365, help="Calendar days of VIX history to fetch"
    )
    parser.add_argument(
        "--indices", nargs="+", default=["NIFTY", "BANKNIFTY", "FINNIFTY"], help="Indices to seed"
    )
    args = parser.parse_args()
    asyncio.run(_run(args.days, args.indices))


if __name__ == "__main__":
    _main()
