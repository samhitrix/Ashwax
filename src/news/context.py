"""News context provider for trade-decision consumers (G9 LLM, breadth gate).

One source feeds all consumers; news can only ADD caution to risk gates, never
remove an existing safety block. State lives in the in-memory shared `news_recent`
cache rebuilt each poll by `_news_loop` (no DB).
"""

from __future__ import annotations

from typing import Any


def build_news_context(symbol: str, shared: dict[str, Any]) -> str | None:
    """One-line news summary for the G9 LLM prompt, or None if nothing recent."""
    rec = shared.get("news_recent", {}).get(symbol)
    if not rec:
        return None
    return f"{rec['sentiment']}/{rec['magnitude']} ({rec['action']}): {rec['headline']} — {rec['reasoning']}"


def macro_gate_thresholds(
    base: float, macro: dict[str, Any] | None, now_ts: float, ttl: float = 14400.0
) -> tuple[float, float]:
    """Breadth-gate (bear_thr, bull_thr) after HIGH-magnitude MACRO news.

    News can only ADD caution: a fresh macro bias LOWERS the counter-trend block
    threshold (making it fire more easily) and never raises the opposite side.
    Returns the unchanged base on both sides when there is no fresh macro bias.
    """
    bear_thr = bull_thr = base
    if macro and (now_ts - macro["ts"]) < ttl:
        if macro["bias"] == "BEARISH":
            bear_thr -= 0.15
        elif macro["bias"] == "BULLISH":
            bull_thr -= 0.15
    return bear_thr, bull_thr
