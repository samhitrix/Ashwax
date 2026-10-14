"""
News API routes (spec §17).

GET /api/news/feed            — latest classified news items
GET /api/news/impact/{symbol} — latest impact status for a symbol
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/news", tags=["news"])

_NEWS_LOG = Path("logs/news_status.json")


def _load_news_log() -> list[dict[str, Any]]:
    try:
        data = json.loads(_NEWS_LOG.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else data.get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@router.get("/feed")
async def news_feed() -> dict[str, Any]:
    """Latest 30 classified news items."""
    items = _load_news_log()
    return {"items": items[-30:], "count": min(len(items), 30)}


@router.get("/impact/{symbol}")
async def news_impact(symbol: str) -> dict[str, Any]:
    """Latest impact status for a given symbol."""
    sym = symbol.upper()
    matches = [i for i in _load_news_log() if i.get("symbol", "").upper() == sym]
    if not matches:
        return {"symbol": sym, "trading_action": "MONITOR", "found": False}
    latest = matches[-1]
    return {
        "symbol": sym,
        "trading_action": latest.get("trading_action", "MONITOR"),
        "sentiment": latest.get("sentiment", "NEUTRAL"),
        "magnitude": latest.get("magnitude", "LOW"),
        "headline": latest.get("headline", ""),
        "found": True,
    }
