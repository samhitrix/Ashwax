"""
Flattrade raw payload -> canonical model translation (market data side).

Pure functions. Field names come from the Phase 0 findings
(docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md):
  - GetQuotes (A3): `lp, h, l, v, bp1..5, sp1..5, bq1..5, sq1..5, ti, ls, uc, lc,
    tsym, token, ltt` (+ `oi` when present). No per-level order counts, no IV/greeks.
  - TPSeries (A3): `time ("DD-MM-YYYY hh:mm:ss"), into, inth, intl, intc, intv, ...`.
  - EODChartData (A3): `time, into, inth, intl, intc, ssboe, intv`; elements may be
    JSON strings.
  - Websocket touchline (A6): `e, tk, ts, ti, ls, pp, lp, pc, v, o, h, l, c, ap, oi,
    poi, toi, bq1, bp1, sq1, sp1, ft, ord_msg`.
All prices are decimal strings in rupees (no paise scaling).

Order side (F3): OrderBook / SingleOrdHist rows, TradeBook, PositionBook, Holdings,
Limits and `Not_Ok` error replies (field lists from findings A3 / A7).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.brokers.base import (
    BidAsk,
    BrokerError,
    Fill,
    Holding,
    Margin,
    MarketDepth,
    OptionQuote,
    Order,
    Position,
    Tick,
)

_log = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
DATE_DTYPE = "datetime64[ns, Asia/Kolkata]"
_TIME_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%d-%b-%Y")  # last: EODChartData "15-OCT-2026"


def to_float(value: Any) -> float:
    """Numeric string -> float; None / '' -> 0.0."""
    if value is None:
        return 0.0
    text = str(value).strip()
    return float(text) if text else 0.0


def parse_time(value: Any) -> datetime:
    """Parse "DD-MM-YYYY hh:mm:ss" / "DD-MM-YYYY" / "DD-MON-YYYY" (IST) or epoch seconds to IST.

    Raises:
        ValueError: the value matches none of the documented forms.
    """
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=_IST)
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_IST)
        except ValueError:
            continue
    raise ValueError(f"unrecognised Flattrade time {value!r}")


def _parse_time_or_now(value: Any) -> datetime:
    """Quote/tick timestamps (`ltt`, `ft`): their exact format is UNVERIFIED, so an
    unparseable or missing value falls back to now rather than failing the quote."""
    if value is None or str(value).strip() == "":
        return datetime.now(_IST)
    try:
        return parse_time(value)
    except ValueError:
        return datetime.now(_IST)


def normalize_depth(symbol: str, quote: dict[str, Any]) -> MarketDepth:
    """GetQuotes reply -> MarketDepth (5 levels; order counts unavailable => 0)."""

    def _side(price_key: str, qty_key: str) -> list[BidAsk]:
        return [
            BidAsk(
                price=to_float(quote.get(f"{price_key}{i}")),
                quantity=int(to_float(quote.get(f"{qty_key}{i}"))),
                orders=0,
            )
            for i in range(1, 6)
            if f"{price_key}{i}" in quote
        ]

    return MarketDepth(
        symbol=symbol,
        bids=_side("bp", "bq"),
        asks=_side("sp", "sq"),
        ltp=to_float(quote.get("lp")),
        timestamp=_parse_time_or_now(quote.get("ltt")),
    )


def normalize_option_quote(symbol: str, quote: dict[str, Any]) -> OptionQuote:
    """GetQuotes reply -> OptionQuote. `oi` only when the reply carries it (else 0);
    prior OI is not in GetQuotes (0). GetQuotes gives no IV or greeks."""
    return OptionQuote(
        symbol=symbol,
        ltp=to_float(quote.get("lp")),
        bid=to_float(quote.get("bp1")),
        ask=to_float(quote.get("sp1")),
        oi=int(to_float(quote.get("oi"))),
        volume=int(to_float(quote.get("v"))),
        prev_oi=0,
    )


def empty_ohlcv() -> pd.DataFrame:
    """Empty frame with the standard columns and a tz-aware date dtype."""
    dtypes = {"date": DATE_DTYPE, "volume": "int64"}
    return pd.DataFrame({c: pd.Series(dtype=dtypes.get(c, "float64")) for c in OHLCV_COLUMNS})


def _candle(el: Any) -> dict[str, Any]:
    row = json.loads(el) if isinstance(el, str) else el
    if not isinstance(row, dict):
        raise ValueError(f"unexpected Flattrade candle element {el!r}")
    stamp = row.get("time")
    if (stamp is None or str(stamp).strip() == "") and row.get("ssboe"):
        stamp = row["ssboe"]
    return {
        "date": parse_time(stamp),
        "open": to_float(row.get("into")),
        "high": to_float(row.get("inth")),
        "low": to_float(row.get("intl")),
        "close": to_float(row.get("intc")),
        "volume": to_float(row.get("intv")),
    }


def candles_to_frame(elements: list[Any], *, daily: bool = False) -> pd.DataFrame:
    """TPSeries / EODChartData elements (objects or JSON strings, any order) ->
    ascending, de-duplicated OHLCV frame (volume int64). A malformed element raises
    ValueError.

    `daily=True` stamps every bar at 00:00 IST (the Kite/Angel convention) BEFORE
    de-duplication, so one trading day never appears under two timestamps (EOD rows
    may carry a time of day or an `ssboe` epoch)."""
    if not elements:
        return empty_ohlcv()
    df = pd.DataFrame([_candle(el) for el in elements], columns=OHLCV_COLUMNS)
    df["date"] = pd.to_datetime(df["date"]).dt.tz_convert(_IST).astype(DATE_DTYPE)
    if daily:
        df["date"] = df["date"].dt.normalize()
    df["volume"] = df["volume"].round().astype("int64")
    return df.drop_duplicates(subset="date", keep="last").sort_values("date").reset_index(drop=True)


def normalize_tick(raw: dict[str, Any]) -> Tick:
    """Websocket touchline message (`tk` snapshot) -> Tick.

    `tf` updates carry only the fields that changed, so a consumer must merge them
    onto the last `tk` snapshot before calling this. A message without `tk` or `lp`
    raises ValueError rather than producing a zero price. `ft` (feed time) is read
    as epoch seconds — its format is UNVERIFIED; unparseable falls back to now.
    """
    for key in ("tk", "lp"):
        if raw.get(key) in (None, ""):
            raise ValueError(f"Flattrade tick without {key!r}: merge tf updates onto tk first")
    return Tick(
        symbol=str(raw.get("ts") or ""),
        instrument_token=str(raw["tk"]),
        ltp=to_float(raw["lp"]),
        bid=to_float(raw.get("bp1")),
        ask=to_float(raw.get("sp1")),
        volume=int(to_float(raw.get("v"))),
        exchange_ts=_parse_time_or_now(raw.get("ft")),
        received_ts=datetime.now(_IST),
    )


# ── Orders / positions / account / errors (F3) ─────────────────────────────────

# Order statuses seen by OpenAlgo (findings A3: inconsistent casing; the vendor doc has
# no enum). Keys are upper-case with "_" and runs of spaces folded to one space.
_STATUS_MAP: dict[str, str] = {
    "OPEN": "OPEN",
    "COMPLETE": "FILLED",
    "REJECTED": "REJECTED",
    "REJECT": "REJECTED",
    "CANCELED": "CANCELLED",
    "CANCELLED": "CANCELLED",
    "TRIGGER PENDING": "OPEN",
    "PENDING": "OPEN",
    "OPEN PENDING": "OPEN",
    "CANCEL PENDING": "OPEN",  # not cancelled until the OMS says so
}
# Not in the canonical OPEN|PARTIAL|FILLED|CANCELLED|REJECTED set on purpose: callers
# (order_lifecycle._BROKER_STATUS_MAP) ignore it, so an unknown status never ends an
# order's life by guess. It is logged at WARNING.
UNKNOWN_STATUS = "UNKNOWN"
TERMINAL_STATUSES: frozenset[str] = frozenset({"FILLED", "CANCELLED", "REJECTED"})

_PRCTYP_TO_CANONICAL: dict[str, str] = {
    "LMT": "LIMIT",
    "SL-LMT": "SL",
    "MKT": "MARKET",
    "SL-MKT": "SL-M",
}
_PRODUCT_FROM_FT: dict[str, str] = {"I": "INTRADAY", "C": "DELIVERY", "M": "MARGIN"}
_SIDE_FROM_FT: dict[str, str] = {"B": "BUY", "S": "SELL"}
_ORDER_TIME_FORMATS = ("%H:%M:%S %d-%m-%Y",)  # Noren `norentm` style; UNVERIFIED

with (Path(__file__).parent / "error_map.json").open(encoding="utf-8") as _f:
    _ERROR_MAP: dict[str, Any] = json.load(_f)


def normalize_status(raw: Any) -> str:
    """Flattrade order status -> canonical status; unknown -> UNKNOWN_STATUS + WARNING."""
    key = " ".join(str(raw or "").replace("_", " ").upper().split())
    status = _STATUS_MAP.get(key)
    if status is None:
        _log.warning(
            "flattrade_unknown_order_status raw=%r -> %s (non-terminal)", raw, UNKNOWN_STATUS
        )
        return UNKNOWN_STATUS
    return status


def canonical_symbol(exch: Any, tsym: Any) -> str:
    """NSE `-EQ` tradingsymbols are exposed by their bare name (as get_instruments does),
    so they match the symbols the rest of the system stores; everything else verbatim."""
    text = str(tsym or "")
    if str(exch or "").upper() == "NSE" and text.endswith("-EQ"):
        return text[: -len("-EQ")]
    return text


def canonical_product(prd: Any) -> str:
    """I/C/M -> INTRADAY/DELIVERY/MARGIN; anything else (H, B, F) is kept verbatim."""
    code = str(prd or "").strip().upper()
    return _PRODUCT_FROM_FT.get(code, code)


def parse_order_time(value: Any) -> datetime | None:
    """`ordenttm` / `norentm` / `fltm` -> IST datetime, or None (formats UNVERIFIED)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return parse_time(value)
    except ValueError:
        pass
    for fmt in _ORDER_TIME_FORMATS:
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=_IST)
        except ValueError:
            continue
    return None


def normalize_order(raw: dict[str, Any]) -> Order:
    """OrderBook / SingleOrdHist row -> Order (findings A3 field list).

    `fillshares` / `avgprc` carry partial fills; an OPEN order with
    0 < fillshares < qty is PARTIAL. Terminal orders have no pending quantity.
    A `trantype` other than B/S raises ValueError (never guess a side).
    """
    side = _SIDE_FROM_FT.get(str(raw.get("trantype") or "").upper())
    if side is None:
        raise ValueError(f"Flattrade order with unknown trantype {raw.get('trantype')!r}")
    qty = int(to_float(raw.get("qty")))
    filled = int(to_float(raw.get("fillshares")))
    status = normalize_status(raw.get("status"))
    if status == "OPEN" and 0 < filled < qty:
        status = "PARTIAL"
    pending = 0 if status in TERMINAL_STATUSES else max(qty - filled, 0)
    prctyp = str(raw.get("prctyp") or "").upper()
    placed_at = parse_order_time(raw.get("ordenttm")) or parse_order_time(raw.get("norentm"))
    return Order(
        broker_order_id=str(raw.get("norenordno") or ""),
        symbol=canonical_symbol(raw.get("exch"), raw.get("tsym")),
        direction=side,
        quantity=qty,
        filled_quantity=filled,
        pending_quantity=pending,
        status=status,
        order_type=_PRCTYP_TO_CANONICAL.get(prctyp, prctyp),
        product=canonical_product(raw.get("prd")),
        average_price=to_float(raw.get("avgprc")),
        tag=str(raw.get("remarks") or ""),
        placed_at=placed_at or datetime.now(_IST),
        exchange_order_id=str(raw.get("exchordid") or ""),
        rejection_reason=str(raw["rejreason"]) if raw.get("rejreason") else None,
        raw=raw,
    )


def normalize_fill(raw: dict[str, Any]) -> Fill:
    """TradeBook row -> Fill. `fltm` format is UNVERIFIED: unparseable -> now (logged)."""
    when = parse_order_time(raw.get("fltm"))
    if when is None:
        _log.warning(
            "flattrade_fill_time_unparsed fltm=%r flid=%s", raw.get("fltm"), raw.get("flid")
        )
        when = datetime.now(_IST)
    return Fill(
        order_id=str(raw.get("norenordno") or ""),
        trade_id=str(raw.get("flid") or ""),
        quantity=int(to_float(raw.get("flqty"))),
        price=to_float(raw.get("flprc")),
        exchange_time=when,
    )


def normalize_position(raw: dict[str, Any]) -> Position:
    """PositionBook row -> Position. pnl = rpnl (realised) + urmtom (unrealised MTM),
    the same total Kite reports; `lp` is the live LTP carried in the row."""
    net_qty = int(to_float(raw.get("netqty")))
    return Position(
        symbol=canonical_symbol(raw.get("exch"), raw.get("tsym")),
        exchange=str(raw.get("exch") or ""),
        direction="long" if net_qty >= 0 else "short",
        quantity=abs(net_qty),
        average_price=to_float(raw.get("netavgprc")),
        last_price=to_float(raw.get("lp")),
        pnl=to_float(raw.get("rpnl")) + to_float(raw.get("urmtom")),
        product=canonical_product(raw.get("prd")),
    )


def holding_scrip(raw: dict[str, Any]) -> dict[str, Any]:
    """The `exch_tsym[]` entry to use for a Holdings row: NSE first, else the first.
    Raises ValueError when there is none (a holding we cannot name must not vanish)."""
    scrips = raw.get("exch_tsym")
    if not isinstance(scrips, list) or not scrips or not all(isinstance(s, dict) for s in scrips):
        raise ValueError(f"Flattrade holding without a usable exch_tsym: {scrips!r}")
    entries: list[dict[str, Any]] = scrips
    for entry in entries:
        if str(entry.get("exch") or "").upper() == "NSE":
            return entry
    return entries[0]


def normalize_holding(raw: dict[str, Any], ltp: float = 0.0) -> Holding:
    """Holdings row -> Holding.

    UNVERIFIED quantity semantics (findings list only the field names): quantity =
    dpqty - usedqty (settled demat shares not sold today, floored at 0) and
    t1_quantity = holdqty + btstqty (treated as not-yet-settled). If holdqty in fact
    already includes dpqty this over-counts, which reconciliation treats as
    still-held: the safe direction (an under-count could orphan real shares).
    """
    scrip = holding_scrip(raw)
    quantity = max(int(to_float(raw.get("dpqty"))) - int(to_float(raw.get("usedqty"))), 0)
    t1 = int(to_float(raw.get("holdqty"))) + int(to_float(raw.get("btstqty")))
    avg = to_float(raw.get("upldprc"))
    return Holding(
        symbol=canonical_symbol(scrip.get("exch"), scrip.get("tsym")),
        exchange=str(scrip.get("exch") or ""),
        quantity=quantity,
        average_price=avg,
        last_price=ltp,
        pnl=(ltp - avg) * (quantity + t1) if ltp else 0.0,
        t1_quantity=t1,
    )


def margin_from_limits(raw: dict[str, Any]) -> Margin:
    """Limits reply -> Margin, mirroring Kite's semantics (_margin_from_kite):

    available_cash = cash - payout - marginused  (free cash, like Kite's
        live_balance; collateral excluded because it cannot buy delivery stock;
        `payin` - today's funds added, not yet cleared - is deliberately EXCLUDED
        so sizing never counts money that may not be usable)
    equity         = available_cash + collateral          (like Kite's `net`)
    used_margin    = marginused ; collateral = collateral

    Fields are strings (findings A3); missing optional fields count as 0. A reply
    without `cash` raises ValueError. How payin/payout enter Flattrade's own
    available-margin figure is UNVERIFIED.
    """
    if raw.get("cash") in (None, ""):
        raise ValueError("Flattrade Limits reply has no cash field")
    cash = to_float(raw.get("cash"))
    used = to_float(raw.get("marginused"))
    collateral = to_float(raw.get("collateral"))
    available = cash - to_float(raw.get("payout")) - used
    return Margin(
        equity=available + collateral,
        available_cash=available,
        used_margin=used,
        collateral=collateral,
    )


def normalize_error(raw: dict[str, Any]) -> BrokerError:
    """`{"stat": "Not_Ok", "emsg": ...}` (or `{"message": <exception text>}`) ->
    BrokerError via error_map.json substring rules (case-insensitive, first match)."""
    message = str(raw.get("emsg") or raw.get("message") or "")
    lowered = message.lower()
    code, retryable = "UNKNOWN", False
    for rule in _ERROR_MAP.get("message_overrides", []):
        if str(rule["contains"]).lower() in lowered:
            code, retryable = str(rule["code"]), bool(rule["retryable"])
            break
    return BrokerError(
        code=code,
        message=message,
        retryable=retryable,
        original_code=str(raw.get("stat") or ""),
        original_message=message,
    )
