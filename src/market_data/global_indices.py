"""Delayed global index quotes for the dashboard header ticker (display only).

Source is Yahoo Finance's public chart endpoint. It is unofficial and can lag 15 minutes
or more, so every row is tagged ``delayed`` and nothing in the trading path reads it.
Results are cached; on a failed refresh the last good rows are returned marked ``stale``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

# Display name -> Yahoo symbol.
GLOBAL_INDICES: dict[str, str] = {
    "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC",
    "DOW JONES": "^DJI",
    "FTSE 100": "^FTSE",
    "DAX": "^GDAXI",
    "NIKKEI 225": "^N225",
    "HANG SENG": "^HSI",
    "SHANGHAI": "000001.SS",
}

CACHE_TTL_S = 60.0
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

FetchOne = Callable[[str], Awaitable[dict[str, Any]]]

_cache: dict[str, Any] = {"ts": 0.0, "rows": []}


async def _fetch_one(symbol: str) -> dict[str, Any]:
    """Return Yahoo's chart ``meta`` block for one symbol."""
    async with (
        aiohttp.ClientSession(headers=_HEADERS) as session,
        session.get(
            _URL.format(sym=symbol.replace("^", "%5E")),
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp,
    ):
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    meta: dict[str, Any] = data["chart"]["result"][0]["meta"]
    return meta


def _row(name: str, meta: dict[str, Any], now_epoch: float) -> dict[str, Any] | None:
    """Build one ticker row from a Yahoo meta block; None when it has no usable price."""
    price = float(meta.get("regularMarketPrice") or 0)
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
    if price <= 0:
        return None
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = period.get("start"), period.get("end")
    is_open = bool(start and end and start <= now_epoch < end)
    change_pct = (price - prev) / prev * 100 if prev > 0 else 0.0
    return {
        "name": name,
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "open": is_open,
    }


async def get_global_indices(
    fetch_one: FetchOne = _fetch_one,
    ttl: float = CACHE_TTL_S,
    now_epoch: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Cached global index rows: ``{"rows": [...], "delayed": True, "stale": bool}``."""
    now_mono = time.monotonic()
    if _cache["rows"] and now_mono - _cache["ts"] < ttl:
        return {"rows": _cache["rows"], "delayed": True, "stale": False}

    results = await asyncio.gather(
        *(fetch_one(sym) for sym in GLOBAL_INDICES.values()), return_exceptions=True
    )
    now = now_epoch()
    rows = []
    for name, res in zip(GLOBAL_INDICES, results, strict=True):
        if isinstance(res, BaseException):
            continue
        try:
            row = _row(name, res, now)
        except (TypeError, ValueError):
            row = None
        if row:
            rows.append(row)

    if rows:
        _cache.update(ts=now_mono, rows=rows)
        return {"rows": rows, "delayed": True, "stale": False}
    # Every symbol failed: serve the last good rows (empty on a cold start), flagged stale.
    return {"rows": _cache["rows"], "delayed": True, "stale": True}
