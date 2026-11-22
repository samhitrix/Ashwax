"""
P&L Bifurcation — splits closed-trade P&L by product bucket for the
Trade & P&L dashboard tab (spec ask: "proper bifurcation of options, CNC & MIS").

Buckets:
  MIS      intraday equity (trades/paper_trades.product == 'MIS')
  CNC      delivery/swing equity (product == 'CNC' or 'NRML' — both are
           non-intraday equity holdings, folded into one bucket since the
           dashboard only asked for a 3-way split)
  OPTIONS  seller-desk options structures (options_positions table)
  ALL      combined total across every bucket above

Pure function, no DB access — callers fetch rows and build TradeRecord list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

_BUCKETS = ("MIS", "CNC", "OPTIONS")


@dataclass
class TradeRecord:
    """Minimal closed-trade record needed for P&L bucketing."""

    product: str  # "MIS" | "CNC" | "NRML" | "OPTIONS"
    net_pnl: float
    exit_ts: datetime


@dataclass
class BucketPnl:
    net_pnl: float = 0.0
    trade_count: int = 0
    weekly: dict[str, float] = field(default_factory=dict)


def _bucket_key(product: str) -> str:
    p = (product or "").upper()
    if p == "MIS":
        return "MIS"
    if p == "OPTIONS":
        return "OPTIONS"
    return "CNC"  # CNC, NRML, or unrecognized equity product


def bucket_pnl(trades: list[TradeRecord]) -> dict[str, dict[str, object]]:
    """Group closed trades into MIS / CNC / OPTIONS / ALL, each with cumulative
    net P&L, trade count, and an ISO-week net-P&L series (same "{year}-W{week:02d}"
    key format as the existing weekly_pnl.json, so the chart component is reused
    as-is)."""
    buckets: dict[str, BucketPnl] = {k: BucketPnl() for k in (*_BUCKETS, "ALL")}

    for t in trades:
        try:
            d = t.exit_ts.isocalendar()
            week_key = f"{d.year}-W{d.week:02d}"
        except Exception:
            continue

        for key in (_bucket_key(t.product), "ALL"):
            b = buckets[key]
            b.net_pnl += t.net_pnl
            b.trade_count += 1
            b.weekly[week_key] = b.weekly.get(week_key, 0.0) + t.net_pnl

    return {
        key: {
            "net_pnl": round(b.net_pnl, 2),
            "trade_count": b.trade_count,
            "weekly": {wk: round(v, 2) for wk, v in b.weekly.items()},
        }
        for key, b in buckets.items()
    }
