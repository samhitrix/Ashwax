"""Correct today's closed live trades to the broker's real fills -- for every live broker.

The safety net behind the exit paths: whatever price a close was booked at, the broker's order
book is the truth. Order books hold today's orders only, so this runs through the session, after
market close and at startup, once per live broker against that broker's own rows.

Matching: the day's fills of one symbol+product are walked in time order and cut
into ROUND TRIPS (flat -> position -> flat). Each closed DB row/leg takes the round trip of its
side and quantity that started nearest its own entry. So a manual exit in the broker terminal is
the exit of the trip it closed, and any extra order on the same strike (a stray agent order,
the broker's auto square-off) forms its own trip, reported as UNTRACKED instead of making the
whole row unverifiable. A row is corrected only when exactly one trip fits it; otherwise it is
left as is and logged, because an invented number is worse than a stale one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.notifications import EventKind, notify
from src.notifications.events import Segment

_LOG = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")

_PRICE_EPSILON = 0.005
_RUPEE_EPSILON = 0.05  # rounding of two 2-decimal columns can differ by paise, never more
# What was last logged per trade: a result is logged once, and again only if it changes.
_logged: dict[str, str] = {}
# DB product spellings -> the canonical Order.product every adapter reports.
_PRODUCT = {"MIS": "INTRADAY", "CNC": "DELIVERY", "NRML": "MARGIN"}


def _log_verify(key: str, line: str, *, level: int = logging.INFO) -> None:
    """One visible line per closed trade/leg: DB vs live. grep `closed_trade_verify`."""
    if _logged.get(key) != line:
        _logged[key] = line
        _LOG.log(level, "closed_trade_verify | %s", line)


def _bare(tradingsymbol: str) -> str:
    return tradingsymbol.split(":")[-1]


def _product(value: Any) -> str | None:
    text = str(value or "").upper()
    return _PRODUCT.get(text, text) or None


@dataclass
class _Trip:
    """One round trip at the broker: entered on `side`, `qty` in and (if closed) `qty` out."""

    side: str
    qty: int = 0
    entry_value: float = 0.0
    exit_qty: int = 0
    exit_value: float = 0.0
    start: datetime | None = None
    end: datetime | None = None  # None = still open at the broker
    carried: bool = False  # opened on an earlier day: the book can't speak for its entry
    claimed: bool = False

    @property
    def entry_px(self) -> float:
        return self.entry_value / self.qty if self.qty and not self.carried else 0.0

    @property
    def exit_px(self) -> float:
        return self.exit_value / self.exit_qty if self.exit_qty else 0.0


def _round_trips(orders: list[Any], start_net: int = 0) -> list[_Trip]:
    """Cut one symbol+product's filled orders (any order) into round trips, in time order.
    `start_net` is the position carried in from an earlier day (signed, + long)."""
    trips: list[_Trip] = []
    net, trip = start_net, None
    if start_net:
        trip = _Trip(side="BUY" if start_net > 0 else "SELL", qty=abs(start_net), carried=True)
    for o in sorted(orders, key=lambda o: o.placed_at):
        left, px = int(o.filled_quantity), float(o.average_price)
        while left > 0:
            if trip is None:
                trip = _Trip(side=o.direction, start=o.placed_at)
            if o.direction == trip.side:  # adds to the position
                trip.qty += left
                trip.entry_value += left * px
                net += left if o.direction == "BUY" else -left
                left = 0
                continue
            take = min(left, abs(net))  # reduces it; the rest (if any) opens the next trip
            trip.exit_qty += take
            trip.exit_value += take * px
            net += take if o.direction == "BUY" else -take
            left -= take
            if net == 0:
                trip.end = o.placed_at
                trips.append(trip)
                trip = None
    if trip is not None:
        trips.append(trip)
    return trips


@dataclass
class _Claim:
    """A closed DB row (equity trade, or one option leg) asking for its broker fills."""

    key: str
    symbol: str
    product: str | None
    side: str
    qty: int
    entered_at: datetime | None
    carried: bool


def _assign(
    claims: list[_Claim], orders: list[Any]
) -> tuple[dict[str, _Trip | str], list[tuple[str, _Trip]]]:
    """Each claim -> its one round trip, or a SKIPPED reason. Also returns the CLOSED round trips
    on these symbols that no claim took (orders the agent has no record of), as (symbol, trip)."""
    out: dict[str, _Trip | str] = {}
    untracked: list[tuple[str, _Trip]] = []
    groups: dict[tuple[str, str | None], list[_Claim]] = {}
    for c in claims:
        groups.setdefault((c.symbol, c.product), []).append(c)
    for (symbol, product), group in groups.items():
        book = [
            o for o in orders if o.symbol == symbol and (product is None or o.product == product)
        ]
        start_net = sum((c.qty if c.side == "BUY" else -c.qty) for c in group if c.carried)
        trips = _round_trips(book, start_net)
        wanted: dict[int, list[_Claim]] = {}
        for c in group:
            fits = [
                t for t in trips if t.side == c.side and t.qty == c.qty and t.carried == c.carried
            ]
            if not fits:
                open_now = any(t.end is None and t.side == c.side for t in trips)
                out[c.key] = "still_open_at_broker" if open_now else "no_matching_fills"
                continue
            if c.carried or c.entered_at is None:
                best = fits[0] if len(fits) == 1 else None
            else:
                at = c.entered_at
                ranked = sorted(
                    fits,
                    key=lambda t: abs((t.start - at).total_seconds()) if t.start else float("inf"),
                )
                best = ranked[0]
            if best is None:
                out[c.key] = "ambiguous_fills"
                continue
            wanted.setdefault(id(best), []).append(c)
            out[c.key] = best
        for cs in wanted.values():
            if len(cs) > 1:  # two rows want the same broker trip: neither is provable
                for c in cs:
                    out[c.key] = "ambiguous_fills"
            else:
                out[cs[0].key].claimed = True  # type: ignore[union-attr]
        for t in trips:
            if t.end is not None and not t.claimed:
                untracked.append((symbol, t))
        for c in group:
            trip = out[c.key]
            if isinstance(trip, _Trip) and trip.end is None:
                out[c.key] = "still_open_at_broker"
    return out, untracked


def _today_ist() -> Any:
    return datetime.now(_IST).date()


def _is_before_today(ts: datetime | None) -> bool:
    return ts is not None and ts.astimezone(_IST).date() < _today_ist()


async def _sweep_equity(
    pool: Any, orders: list[Any], broker_name: str
) -> tuple[list[str], list[tuple[str, _Trip]]]:
    from src.risk.real_pnl import ProductType, calculate_real_pnl

    rows = await pool.fetch(
        "SELECT agent_trade_id, symbol, direction, product, quantity, partial_qty_booked, "
        "fill_price, exit_price, pnl_realized, charges_taxes, "
        "COALESCE(entry_timestamp, fill_timestamp, created_at) AS entered_at "
        "FROM trades WHERE order_state = 'CLOSED' "
        "AND (exit_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
        "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date "
        "AND COALESCE(exec_broker, 'kite') = $1",
        broker_name,
    )
    claims = [
        _Claim(
            key=r["agent_trade_id"],
            symbol=r["symbol"],
            product=_product(r["product"]),
            side="BUY" if str(r["direction"]).lower() == "long" else "SELL",
            qty=int(r["quantity"] or 0) + int(r["partial_qty_booked"] or 0),
            entered_at=r.get("entered_at"),
            carried=_is_before_today(r.get("entered_at")),
        )
        for r in rows
    ]
    assigned, untracked = _assign(claims, orders)

    corrected: list[str] = []
    for r, c in zip(rows, claims, strict=True):
        symbol, tid, qty = r["symbol"], r["agent_trade_id"], c.qty
        trip = assigned[tid]
        if isinstance(trip, str):
            _log_verify(tid, f"result=SKIPPED reason={trip} symbol={symbol} tid={tid} db_qty={qty}")
            continue
        is_long = c.side == "BUY"
        entry_px = trip.entry_px or float(r["fill_price"] or 0)
        exit_px = trip.exit_px
        if not entry_px:
            _log_verify(tid, f"result=SKIPPED reason=no_entry_price symbol={symbol} tid={tid}")
            continue
        old_entry, old_exit = float(r["fill_price"] or 0), float(r["exit_price"] or 0)
        db_net = float(r["pnl_realized"] or 0)
        charges = float(r["charges_taxes"] or 0)
        db_gross = db_net + charges
        live_gross = (exit_px - entry_px) * qty * (1 if is_long else -1)
        detail = (
            f"symbol={symbol} tid={tid} qty={qty} "
            f"db_entry={old_entry:.2f} live_entry={entry_px:.2f} "
            f"db_exit={old_exit:.2f} live_exit={exit_px:.2f} "
            f"db_gross={db_gross:.2f} live_gross={live_gross:.2f} "
            f"charges={charges:.2f} db_net={db_net:.2f}"
        )
        if (
            abs(old_entry - entry_px) < _PRICE_EPSILON
            and abs(old_exit - exit_px) < _PRICE_EPSILON
            and abs(db_gross - live_gross) < _RUPEE_EPSILON
        ):
            _log_verify(tid, f"result=MATCH {detail}")
            continue
        product = ProductType.INTRADAY if r["product"] == "MIS" else ProductType.DELIVERY
        pnl = calculate_real_pnl(
            symbol, entry_px, exit_px, qty, product, is_short=not is_long, broker=broker_name
        )
        await pool.execute(
            "UPDATE trades SET fill_price = $2, exit_price = $3, pnl_realized = $4, "
            "charges_taxes = $5, charges_breakdown = $6::jsonb, updated_at = NOW() "
            "WHERE agent_trade_id = $1",
            tid,
            round(entry_px, 4),
            round(exit_px, 4),
            round(pnl.net_pnl, 2),
            round(pnl.costs.total, 2),
            json.dumps(pnl.costs.as_dict()),
        )
        _log_verify(
            tid,
            f"result=CORRECTED {detail} new_net={pnl.net_pnl:.2f} new_charges={pnl.costs.total:.2f}",
            level=logging.WARNING,
        )
        corrected.append(
            f"{symbol}: exit {old_exit:.2f} → {exit_px:.2f}, gross {db_gross:.2f} → {live_gross:.2f}"
        )
    return corrected, untracked


async def _sweep_options(
    pool: Any, orders: list[Any], broker_name: str
) -> tuple[list[str], list[tuple[str, _Trip]]]:
    from src.risk.real_pnl import options_trade_charges

    rows = await pool.fetch(
        "SELECT position_id, legs, realized_pnl, opened_at FROM options_positions "
        "WHERE status = 'CLOSED' "
        "AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date "
        "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date "
        "AND COALESCE(is_paper, FALSE) = FALSE AND COALESCE(exec_broker, 'kite') = $1",
        broker_name,
    )
    parsed: list[tuple[Any, dict[str, Any], list[tuple[dict[str, Any], _Claim]]]] = []
    claims: list[_Claim] = []
    for r in rows:
        raw = r["legs"]
        legs = json.loads(raw) if isinstance(raw, str) else raw
        opened = r.get("opened_at")
        per: list[tuple[dict[str, Any], _Claim]] = []
        for i, leg in enumerate(
            leg for group in ("protection", "income", "closed") for leg in legs.get(group, [])
        ):
            c = _Claim(
                key=f"{r['position_id']}#{i}",
                symbol=_bare(str(leg["tradingsymbol"])),
                product=_product(leg.get("product")),
                side=str(leg["direction"]),
                qty=int(leg.get("lots", 1)) * int(leg.get("lot_size", 1)),
                entered_at=opened,
                carried=_is_before_today(opened),
            )
            per.append((leg, c))
            claims.append(c)
        parsed.append((r, legs, per))
    assigned, untracked = _assign(claims, orders)

    corrected: list[str] = []
    for r, legs, per in parsed:
        fills: list[tuple[dict[str, Any], float, float, int]] = []
        for leg, c in per:
            trip = assigned[c.key]
            entry_px = (
                (trip.entry_px or float(leg.get("premium") or 0))
                if isinstance(trip, _Trip)
                else 0.0
            )
            if isinstance(trip, str) or not entry_px:
                _log_verify(
                    f"{r['position_id']}:{c.symbol}",
                    f"result=SKIPPED reason={trip if isinstance(trip, str) else 'no_entry_price'} "
                    f"position={r['position_id']} symbol={c.symbol} db_qty={c.qty}",
                )
                break
            fills.append((leg, entry_px, trip.exit_px, c.qty))
        if len(fills) != len(per) or not fills:
            continue
        realized = sum(
            (1.0 if leg["direction"] == "BUY" else -1.0) * (exit_px - entry_px) * qty
            for leg, entry_px, exit_px, qty in fills
        )
        old = float(r["realized_pnl"]) if r["realized_pnl"] is not None else None
        changed = any(
            abs(float(leg.get("premium") or 0) - entry_px) >= _PRICE_EPSILON
            or abs(float(leg.get("exit_premium") or 0) - exit_px) >= _PRICE_EPSILON
            for leg, entry_px, exit_px, _ in fills
        ) or (old is None or abs(old - realized) >= _RUPEE_EPSILON)
        for leg, entry_px, exit_px, qty in fills:
            _log_verify(
                f"{r['position_id']}:{leg['tradingsymbol']}",
                f"result={'CORRECTED' if changed else 'MATCH'} position={r['position_id']} "
                f"symbol={_bare(str(leg['tradingsymbol']))} qty={qty} "
                f"db_entry={float(leg.get('premium') or 0):.2f} live_entry={entry_px:.2f} "
                f"db_exit={float(leg.get('exit_premium') or 0):.2f} live_exit={exit_px:.2f}",
                level=logging.WARNING if changed else logging.INFO,
            )
        _log_verify(
            str(r["position_id"]),
            f"result={'CORRECTED' if changed else 'MATCH'} position={r['position_id']} "
            f"db_gross={old if old is not None else 'none'} live_gross={realized:.2f}",
            level=logging.WARNING if changed else logging.INFO,
        )
        if not changed:
            continue
        for leg, entry_px, exit_px, _ in fills:
            leg["premium"] = round(entry_px, 4)
            leg["exit_premium"] = round(exit_px, 4)
            leg.pop("exit_estimated", None)
        charges = options_trade_charges(
            [(str(leg["direction"]), ep, xp, q) for leg, ep, xp, q in fills], broker_name
        )
        await pool.execute(
            "UPDATE options_positions SET legs = $2::jsonb, realized_pnl = $3, "
            "charges_taxes = $4, charges_breakdown = $5::jsonb WHERE position_id = $1",
            r["position_id"],
            json.dumps(legs),
            round(realized, 2),
            round(charges.total, 2),
            json.dumps(charges.as_dict()),
        )
        corrected.append(
            f"{r['position_id']}: P&L {old if old is not None else 'none'} → {realized:.2f}"
        )
    return corrected, untracked


def _report_untracked(untracked: list[tuple[str, _Trip]], broker_name: str) -> list[str]:
    """Round trips the broker filled on a symbol the agent traded today but that no agent row
    accounts for (a stray agent order, a broker auto square-off of it, a manual trade): real
    money, so logged WARNING and sent once each."""
    lines: list[str] = []
    for symbol, t in untracked:
        sign = 1.0 if t.side == "BUY" else -1.0
        gross = sign * (t.exit_px - t.entry_px) * t.qty
        start = t.start.astimezone(_IST).strftime("%H:%M:%S") if t.start else "carried"
        end = t.end.astimezone(_IST).strftime("%H:%M:%S") if t.end else "-"
        line = (
            f"{symbol} {t.side} {t.qty} @ {t.entry_px:.2f} ({start}) -> "
            f"{t.exit_px:.2f} ({end}) gross {gross:.2f}"
        )
        key = f"untracked:{broker_name}:{line}"
        if _logged.get(key) == line:
            continue
        _log_verify(
            key,
            f"result=UNTRACKED broker={broker_name} {line} -- broker fills with no agent row",
            level=logging.WARNING,
        )
        lines.append(line)
    return lines


async def reconcile_closed_fills(pool: Any, broker: Any, broker_name: str = "kite") -> list[str]:
    """Correct today's closed live rows of `broker_name` to that broker's fills; returns what
    changed. Idempotent: a second run finds nothing to correct, and sends no message.
    """
    orders = [
        o for o in await broker.get_all_orders() if o.status == "FILLED" and o.filled_quantity
    ]
    corrected, untracked = await _sweep_equity(pool, orders, broker_name)
    opt_corrected, opt_untracked = await _sweep_options(pool, orders, broker_name)
    corrected += opt_corrected
    stray = _report_untracked(untracked + opt_untracked, broker_name)
    if corrected:
        _LOG.warning(
            "closed_fill_sweep_corrected broker=%s count=%d %s",
            broker_name,
            len(corrected),
            corrected,
        )
    for title, items in (
        ("PRICES CORRECTED FROM BROKER", corrected),
        ("UNTRACKED BROKER TRADES", stray),
    ):
        if not items:
            continue
        try:
            notify(
                EventKind.STRATEGY_ERROR,
                is_paper=False,
                segment=Segment.INTRADAY,
                dedupe_key=f"closed_fill_sweep:{broker_name}:{title}:{'|'.join(items)}",
                title=f"{title} ({broker_name})",
                strategy=broker_name,
                reason="\n".join(items)[:800],
            )
        except Exception:  # noqa: BLE001 -- notify() must never fail the sweep
            pass
    return corrected
