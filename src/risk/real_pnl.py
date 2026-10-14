"""
Real P&L calculator — all-in after-cost P&L for NSE equity trades.

Charge structure (Zerodha / NSE equity, FY2026):
  Brokerage        ₹20 per executed order (buy + sell = ₹40), capped at 0.03% of turnover
  STT              Delivery: 0.10% on sell value | Intraday: 0.025% on sell value
  Exchange (NSE)   0.00297% of turnover (buy + sell)
  SEBI             ₹10 per crore of turnover
  Stamp duty       Delivery: 0.015% on buy value | Intraday: 0.003% on buy value
  GST              18% on (brokerage + exchange + SEBI)
  DP charges       ₹15.93 per distinct scrip sold (delivery only, per debit from demat)

All amounts in INR. Quantity is in shares.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ProductType(StrEnum):
    DELIVERY = "CNC"  # CNC — held overnight, STT on sell side only
    INTRADAY = "MIS"  # MIS — squared off same day


# --- charge constants ---
# Defaults below; config/charges.json overrides any key (rates change by regulation).
_CHARGE_DEFAULTS: dict[str, float] = {
    "brokerage_flat": 20.0,  # ₹ per order leg
    "brokerage_max_pct": 0.0003,  # 0.03% of turnover per leg
    "stt_delivery_sell_pct": 0.001,  # 0.10%
    "stt_intraday_sell_pct": 0.00025,  # 0.025%
    "exchange_pct": 0.0000297,  # NSE equity
    "sebi_per_crore": 10.0,
    "stamp_delivery_buy_pct": 0.00015,  # 0.015%
    "stamp_intraday_buy_pct": 0.00003,  # 0.003%
    "gst_rate": 0.18,
    "dp_charge_per_scrip": 15.93,  # charged once per scrip per debit day
}
_OPTIONS_DEFAULTS: dict[str, float] = {
    "brokerage_flat": 20.0,  # ₹ per executed order
    "stt_sell_pct": 0.0015,  # on premium sold
    "exchange_pct": 0.0003553,  # NSE F&O options, on premium turnover
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00003,  # on premium bought
    "gst_rate": 0.18,
}
_DEFAULTS_BY_SEGMENT = {"equity": _CHARGE_DEFAULTS, "options": _OPTIONS_DEFAULTS}
_CHARGES_PATH = Path(__file__).parent.parent.parent / "config" / "charges.json"
_GLOBAL_PATH = Path(__file__).parent.parent.parent / "config" / "global.json"
_log = logging.getLogger(__name__)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("charges_config_unreadable path=%s err=%s", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _resolve_rates(raw: dict[str, Any], broker: str, segment: str) -> dict[str, float]:
    """Defaults overlaid with charges.json brokers[broker][segment].

    The legacy flat file (no "brokers" key) is read as the equity block of every broker.
    """
    rates = dict(_DEFAULTS_BY_SEGMENT[segment])
    if "brokers" in raw:
        block = (raw["brokers"].get(broker) or {}).get(segment) or {}
    else:
        block = raw if segment == "equity" else {}
    for key in rates:
        if key in block:
            rates[key] = float(block[key])
    return rates


def _load_charges(
    path: Path = _CHARGES_PATH, broker: str = "kite", segment: str = "equity"
) -> dict[str, float]:
    return _resolve_rates(_read_json(path), broker, segment)


def active_broker(for_paper: bool = False) -> str:
    """Broker whose rate card applies: global.json broker.name (env BROKER wins).

    The paper adapter has no fees of its own, so a "paper" broker (or for_paper)
    uses charges.json paper_rates_broker.
    """
    name = os.getenv("BROKER") or str(
        _read_json(_GLOBAL_PATH).get("broker", {}).get("name", "paper")
    )
    if for_paper or name == "paper":
        return str(_read_json(_CHARGES_PATH).get("paper_rates_broker", "kite"))
    return name


_cache: dict[tuple[str, str], tuple[float, dict[str, float]]] = {}


def rates(segment: str, broker: str | None = None) -> dict[str, float]:
    """Rate card for (broker, segment); re-read whenever charges.json changes (no restart)."""
    broker = broker or active_broker()
    try:
        mtime = _CHARGES_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    hit = _cache.get((broker, segment))
    if hit is None or hit[0] != mtime:
        hit = (mtime, _load_charges(_CHARGES_PATH, broker, segment))
        _cache[(broker, segment)] = hit
    return hit[1]


@dataclass
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_charge: float
    sebi_charge: float
    stamp_duty: float
    gst: float
    dp_charge: float

    @property
    def total(self) -> float:
        return (
            self.brokerage
            + self.stt
            + self.exchange_charge
            + self.sebi_charge
            + self.stamp_duty
            + self.gst
            + self.dp_charge
        )

    def add(self, other: CostBreakdown) -> CostBreakdown:
        """Sum two breakdowns (partial slices / option legs into one trade total)."""
        return CostBreakdown(
            self.brokerage + other.brokerage,
            self.stt + other.stt,
            self.exchange_charge + other.exchange_charge,
            self.sebi_charge + other.sebi_charge,
            self.stamp_duty + other.stamp_duty,
            self.gst + other.gst,
            self.dp_charge + other.dp_charge,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "exchange": self.exchange_charge,
            "sebi": self.sebi_charge,
            "stamp": self.stamp_duty,
            "gst": self.gst,
            "dp": self.dp_charge,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CostBreakdown:
        return cls(
            float(d.get("brokerage", 0)),
            float(d.get("stt", 0)),
            float(d.get("exchange", 0)),
            float(d.get("sebi", 0)),
            float(d.get("stamp", 0)),
            float(d.get("gst", 0)),
            float(d.get("dp", 0)),
        )


@dataclass
class RealPnL:
    gross_pnl: float  # (exit_price - entry_price) × quantity; negative for short
    costs: CostBreakdown
    net_pnl: float  # gross_pnl - costs.total
    net_pnl_pct: float  # net_pnl / invested_capital


def _brokerage_for_leg(turnover: float, c: dict[str, float]) -> float:
    """₹20 flat or 0.03% cap, whichever is lower."""
    return min(c["brokerage_flat"], turnover * c["brokerage_max_pct"])


def options_leg_charges(
    side: str, premium: float, qty: int, broker: str | None = None
) -> CostBreakdown:
    """Charges for ONE executed option order (a buy or a sell of `qty` units at `premium`).

    Brokerage is flat per order; STT is on the sell side only; stamp on the buy side only.
    """
    c = rates("options", broker)
    value = premium * qty
    brokerage = c["brokerage_flat"] if value > 0 else 0.0
    stt = value * c["stt_sell_pct"] if side.upper() == "SELL" else 0.0
    exchange = value * c["exchange_pct"]
    sebi = value / 1_00_00_000 * c["sebi_per_crore"]
    stamp = value * c["stamp_buy_pct"] if side.upper() == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * c["gst_rate"]
    return CostBreakdown(
        round(brokerage, 2),
        round(stt, 2),
        round(exchange, 4),
        round(sebi, 4),
        round(stamp, 4),
        round(gst, 2),
        0.0,
    )


def calculate_real_pnl(
    symbol: str,  # noqa: ARG001 — kept for logging context
    entry_price: float,
    exit_price: float,
    quantity: int,
    product: ProductType,
    is_short: bool = False,
) -> RealPnL:
    """
    Compute all-in after-cost P&L for a single completed trade.

    Args:
        symbol: Instrument symbol (unused in arithmetic, available for callers).
        entry_price: Actual fill price on entry (buy for long, sell for short).
        exit_price: Actual fill price on exit.
        quantity: Number of shares.
        product: CNC (delivery) or MIS (intraday).
        is_short: True for short trades (sell first, buy later).

    Returns:
        RealPnL with full cost breakdown.
    """
    c = rates("equity")
    if is_short:
        buy_value = exit_price * quantity
        sell_value = entry_price * quantity
    else:
        buy_value = entry_price * quantity
        sell_value = exit_price * quantity

    turnover = buy_value + sell_value

    # Brokerage: one leg each for buy and sell
    brokerage = _brokerage_for_leg(buy_value, c) + _brokerage_for_leg(sell_value, c)

    # STT
    if product == ProductType.DELIVERY:
        stt = sell_value * c["stt_delivery_sell_pct"]
    else:
        stt = sell_value * c["stt_intraday_sell_pct"]

    # Exchange transaction charge
    exchange_charge = turnover * c["exchange_pct"]

    # SEBI charge
    sebi_charge = (turnover / 1_00_00_000) * c["sebi_per_crore"]

    # Stamp duty — on buy leg only
    if product == ProductType.DELIVERY:
        stamp_duty = buy_value * c["stamp_delivery_buy_pct"]
    else:
        stamp_duty = buy_value * c["stamp_intraday_buy_pct"]

    # GST on brokerage + exchange + SEBI (not on STT or stamp)
    gst = (brokerage + exchange_charge + sebi_charge) * c["gst_rate"]

    # DP charge: delivery only, charged once per scrip per debit transaction
    dp_charge = c["dp_charge_per_scrip"] if product == ProductType.DELIVERY else 0.0

    costs = CostBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_charge=round(exchange_charge, 4),
        sebi_charge=round(sebi_charge, 4),
        stamp_duty=round(stamp_duty, 4),
        gst=round(gst, 2),
        dp_charge=round(dp_charge, 2),
    )

    if is_short:
        gross_pnl = (entry_price - exit_price) * quantity
    else:
        gross_pnl = (exit_price - entry_price) * quantity

    invested_capital = entry_price * quantity
    net_pnl = gross_pnl - costs.total
    net_pnl_pct = net_pnl / invested_capital if invested_capital else 0.0

    return RealPnL(
        gross_pnl=round(gross_pnl, 2),
        costs=costs,
        net_pnl=round(net_pnl, 2),
        net_pnl_pct=round(net_pnl_pct, 6),
    )


def options_trade_charges(
    legs: list[tuple[str, float, float, int]], broker: str | None = None
) -> CostBreakdown:
    """Whole-structure charges: each leg is (entry_direction, entry_premium, exit_premium, qty).

    A leg is two executed orders - the entry (in its own direction) and the exit (opposite side).
    """
    total = CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    for direction, entry, exit_, qty in legs:
        exit_side = "SELL" if direction.upper() == "BUY" else "BUY"
        total = total.add(options_leg_charges(direction, entry, qty, broker))
        total = total.add(options_leg_charges(exit_side, exit_, qty, broker))
    return total
