"""One-time VIX-proxy IV-history bootstrap (options_spec.md §13, Phase 1).

IVR needs 252 days of historical IV; on day 1 there is none. This seeds
``options_vol_intel_history`` from a downloaded India VIX historical CSV
(NSE's own historical-data export: ``Date,Open,High,Low,Close,...`` — only
``Date`` and ``Close`` are used here), scaled per index, with
``confidence='VIX_PROXY'``. Only ``iv_30d`` and an expanding-window ``ivr``
are populated — realized-vol/IVP/term-structure/skew/GEX genuinely don't
exist from a VIX-only proxy and are left ``NULL`` rather than fabricated.

Run once: ``python -m src.options.bootstrap_iv_history --vix-csv <path>``
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from src.market import sessions as _sessions
from src.options.volatility_formulas import iv_rank

if TYPE_CHECKING:
    import asyncpg

    from src.db.repositories.options import OptionsRepository

_LOG = logging.getLogger("options.bootstrap_iv_history")
_IST = ZoneInfo("Asia/Kolkata")
_CLOSE_TIME = _sessions.hhmm(_sessions.market_config("nse_fo")["iv_history_close"])

_BANKNIFTY_SCALING_DEFAULT = 1.25
_FINNIFTY_SCALING_DEFAULT = 1.15

_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y")


def _parse_date(raw: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {raw!r}")


def parse_vix_csv(
    path: Path, *, date_col: str = "Date", close_col: str = "Close"
) -> list[tuple[date, float]]:
    """Parse an NSE India VIX historical-data CSV into ``(date, vix_close)`` pairs.

    Sorted ascending by date. ``vix_close`` is the raw annualized-percentage
    VIX value (e.g. 13.25 for 13.25%) — callers convert to a decimal fraction.
    """
    rows: list[tuple[date, float]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if date_col not in row or close_col not in row:
                raise ValueError(
                    f"CSV missing expected columns {date_col!r}/{close_col!r}; "
                    f"found: {list(row.keys())}"
                )
            d = _parse_date(row[date_col])
            close = float(row[close_col].strip().replace(",", ""))
            rows.append((d, close))
    rows.sort(key=lambda r: r[0])
    return rows


def scale_for_index(
    vix: float,
    underlying: str,
    *,
    banknifty_scaling: float = _BANKNIFTY_SCALING_DEFAULT,
    finnifty_scaling: float = _FINNIFTY_SCALING_DEFAULT,
) -> float:
    """Index-specific IV estimate from raw VIX (options_spec.md §13)."""
    root = underlying.upper()
    if root == "BANKNIFTY":
        return vix * banknifty_scaling
    if root == "FINNIFTY":
        return vix * finnifty_scaling
    return vix  # NIFTY: VIX as-is


async def seed_from_series(
    repo: OptionsRepository,
    vix_rows: list[tuple[date, float]],
    indices: list[str],
    *,
    banknifty_scaling: float = _BANKNIFTY_SCALING_DEFAULT,
    finnifty_scaling: float = _FINNIFTY_SCALING_DEFAULT,
) -> int:
    """Seed options_vol_intel_history for every index from a ``(date, vix_close)``
    series (ascending by date). Shared by the CSV bootstrap (``bootstrap()``) and
    any other VIX-history source (e.g. broker historical data)."""
    inserted = 0

    for underlying in indices:
        scaled_series: list[float] = []
        for d, vix_close in vix_rows:
            scaled_pct = scale_for_index(
                vix_close,
                underlying,
                banknifty_scaling=banknifty_scaling,
                finnifty_scaling=finnifty_scaling,
            )
            iv_decimal = scaled_pct / 100.0
            history_before = list(scaled_series)  # expanding window, excludes today
            ivr = iv_rank(iv_decimal, history_before) if history_before else None
            scaled_series.append(iv_decimal)

            timestamp = datetime.combine(d, _CLOSE_TIME, tzinfo=_IST)
            await repo.insert_vol_intel(
                underlying,
                timestamp,
                iv_30d=iv_decimal,
                iv_realized_20d=None,
                iv_realized_30d=None,
                iv_realized_60d=None,
                ivr=ivr,
                ivp=None,
                vol_premium=None,
                term_structure=None,
                skew_25delta=None,
                gex=None,
                confidence="VIX_PROXY",
            )
            inserted += 1

    _LOG.info("bootstrap_iv_history_done rows=%d indices=%s", inserted, indices)
    return inserted


async def bootstrap(
    pool: asyncpg.Pool,
    vix_csv: Path,
    indices: list[str],
    *,
    banknifty_scaling: float = _BANKNIFTY_SCALING_DEFAULT,
    finnifty_scaling: float = _FINNIFTY_SCALING_DEFAULT,
) -> int:
    """Seed options_vol_intel_history for every index from the VIX CSV. Returns rows inserted."""
    from src.db.repositories.options import OptionsRepository

    repo = OptionsRepository(pool)
    vix_rows = parse_vix_csv(vix_csv)
    return await seed_from_series(
        repo,
        vix_rows,
        indices,
        banknifty_scaling=banknifty_scaling,
        finnifty_scaling=finnifty_scaling,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vix-csv", required=True, type=Path, help="NSE VIX historical CSV path")
    parser.add_argument(
        "--indices", nargs="+", default=["NIFTY", "BANKNIFTY", "FINNIFTY"], help="Indices to seed"
    )
    args = parser.parse_args()

    async def _run() -> None:
        from src.db.pool import init_pool

        pool = await init_pool()
        count = await bootstrap(pool, args.vix_csv, args.indices)
        print(f"Inserted {count} rows into options_vol_intel_history.")

    asyncio.run(_run())


if __name__ == "__main__":
    _main()
