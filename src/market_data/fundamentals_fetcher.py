"""Per-symbol fundamentals snapshot via yfinance (NSE `.NS` suffix).

MVP fundamentals data source (config/global.json `fundamentals.enabled`,
default false). yfinance is free and unofficial — coverage/accuracy for
Indian small/mid-caps is known to be inconsistent, so every field is optional
and a bad/missing symbol never aborts the batch (same fail-soft convention as
sector_constituents.py's per-sector fetch). This module ONLY fetches and
returns data — it never touches a CRM gate or the G9 prompt; see
FundamentalsRepository / orchestrator.py's shadow-attach call site for how the
result is used (log-only until scripts/backtest_fundamentals_filter.py
validates it).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger(__name__)  # "src.market_data.fundamentals_fetcher" -> market_data.log

# .info fields pulled, all optional — missing ones become None, never raise.
_FIELDS = (
    "trailingPE",
    "forwardPE",
    "trailingEps",
    "forwardEps",
    "earningsGrowth",
    "revenueGrowth",
    "returnOnEquity",
    "debtToEquity",
    "marketCap",
    "dividendYield",
    "sector",
)


def _fetch_one_symbol_fundamentals(symbol: str) -> dict[str, Any] | None:
    try:
        import yfinance as yf

        info = yf.Ticker(f"{symbol}.NS").info
        if not info:
            _log.warning("fundamentals_fetch_empty symbol=%s err=yfinance returned no info", symbol)
            return None
        return {"symbol": symbol, **{f: info.get(f) for f in _FIELDS}}
    except Exception as exc:
        _log.warning("fundamentals_fetch_failed symbol=%s err=%s", symbol, exc)
        return None


async def fetch_fundamentals_batch(
    symbols: list[str], max_concurrent: int = 5
) -> list[dict[str, Any]]:
    """Fetch fundamentals for many symbols concurrently (yfinance's `.info` is
    blocking HTTP, so each call runs in a thread under a semaphore). Returns
    only successful rows — a symbol yfinance can't resolve is silently skipped,
    not an error for the caller."""
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(sym: str) -> dict[str, Any] | None:
        async with sem:
            return await asyncio.to_thread(_fetch_one_symbol_fundamentals, sym)

    results = await asyncio.gather(*(_one(s) for s in symbols))
    rows = [r for r in results if r is not None]
    failed = [s for s, r in zip(symbols, results, strict=True) if r is None]
    # marketCap feeds universe_filter.max_market_cap_crores — a row without it is
    # kept unfiltered by the swing scan, so say how many there are.
    no_cap = [r["symbol"] for r in rows if r.get("marketCap") is None]
    _log.info(
        "fundamentals_fetch_batch ok=%d/%d failed=%d no_market_cap=%d",
        len(rows),
        len(symbols),
        len(failed),
        len(no_cap),
    )
    if failed:
        _log.warning(
            "fundamentals_fetch_batch_failed_symbols n=%d first=%s", len(failed), failed[:20]
        )
    if no_cap:
        _log.warning("fundamentals_no_market_cap n=%d first=%s", len(no_cap), no_cap[:20])
    return rows
