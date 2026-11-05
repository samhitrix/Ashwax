"""Correct today's closed live trades to the broker's real fills.

The safety net behind the exit paths: whatever price a close was booked at, the broker's order
book is the truth. Kite's order book holds today's orders only, so this runs after market close
and at startup. A row is corrected only when the book identifies its orders unambiguously;
otherwise it is left as is and logged, because an invented number is worse than a stale one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.notifications import EventKind, notify
from src.notifications.events import Segment

_LOG = logging.getLogger(__name__)

_PRICE_EPSILON = 0.005
_RUPEE_EPSILON = 0.05  # rounding of two 2-decimal columns can differ by paise, never more
# What was last logged per trade: a result is logged once, and again only if it changes.
_logged: dict[str, str] = {}


def _log_verify(key: str, line: str, *, level: int = logging.INFO) -> None:
    """One visible line per closed trade/leg: DB vs live. grep `closed_trade_verify`."""
    if _logged.get(key) != line:
        _logged[key] = line
        _LOG.log(level, "closed_trade_verify | %s", line)


def _avg_fill(orders: list[Any]) -> tuple[int, float]:
    """(total filled qty, quantity-weighted average price)."""
    qty = sum(int(o.filled_quantity) for o in orders)
    if not qty:
        return 0, 0.0
    return qty, sum(int(o.filled_quantity) * float(o.average_price) for o in orders) / qty


def _bare(tradingsymbol: str) -> str:
    return tradingsymbol.split(":")[-1]


async def _sweep_equity(pool: Any, orders: list[Any], broker_name: str) -> list[str]:
    from src.risk.real_pnl import ProductType, calculate_real_pnl

    rows = await pool.fetch(
        "SELECT agent_trade_id, symbol, direction, product, quantity, partial_qty_booked, "
        "fill_price, exit_price, pnl_realized, charges_taxes "
        "FROM trades WHERE order_state = 'CLOSED' "
        "AND (exit_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
        "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date "
        "AND COALESCE(exec_broker, 'kite') = $1",
        broker_name,
    )
    per_symbol: dict[str, int] = {}
    for r in rows:
        per_symbol[r["symbol"]] = per_symbol.get(r["symbol"], 0) + 1

    corrected: list[str] = []
    for r in rows:
        symbol, tid = r["symbol"], r["agent_trade_id"]
        if per_symbol[symbol] > 1:
            _log_verify(
                tid, f"result=SKIPPED reason=same_symbol_twice_today symbol={symbol} tid={tid}"
            )
            continue
        is_long = str(r["direction"]).lower() == "long"
        qty = int(r["quantity"] or 0) + int(r["partial_qty_booked"] or 0)
        entry_side, exit_side = ("BUY", "SELL") if is_long else ("SELL", "BUY")
        same = [o for o in orders if o.symbol == symbol]
        exit_qty, exit_px = _avg_fill([o for o in same if o.direction == exit_side])
        entry_qty, entry_px = _avg_fill([o for o in same if o.direction == entry_side])
        if entry_qty == 0:  # opened on an earlier day: the book can't speak for the entry
            entry_px = float(r["fill_price"] or 0)
        elif entry_qty != qty:
            _log_verify(
                tid,
                f"result=SKIPPED reason=entry_qty_differs symbol={symbol} tid={tid} "
                f"db_qty={qty} live_qty={entry_qty}",
            )
            continue
        if exit_qty != qty or not entry_px:
            _log_verify(
                tid,
                f"result=SKIPPED reason=exit_qty_differs symbol={symbol} tid={tid} "
                f"db_qty={qty} live_qty={exit_qty}",
            )
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
    return corrected


async def _sweep_options(pool: Any, orders: list[Any], broker_name: str) -> list[str]:
    from src.risk.real_pnl import options_trade_charges

    rows = await pool.fetch(
        "SELECT position_id, legs, realized_pnl FROM options_positions WHERE status = 'CLOSED' "
        "AND (closed_at AT TIME ZONE 'Asia/Kolkata')::date "
        "= (NOW() AT TIME ZONE 'Asia/Kolkata')::date "
        "AND COALESCE(is_paper, FALSE) = FALSE AND COALESCE(exec_broker, 'kite') = $1",
        broker_name,
    )
    corrected: list[str] = []
    for r in rows:
        raw = r["legs"]
        legs = json.loads(raw) if isinstance(raw, str) else raw
        all_legs = [
            leg for group in ("protection", "income", "closed") for leg in legs.get(group, [])
        ]
        fills: list[tuple[dict[str, Any], float, float, int]] = []
        for leg in all_legs:
            symbol = _bare(str(leg["tradingsymbol"]))
            qty = int(leg.get("lots", 1)) * int(leg.get("lot_size", 1))
            side = str(leg["direction"])
            same = [o for o in orders if o.symbol == symbol]
            entry_qty, entry_px = _avg_fill([o for o in same if o.direction == side])
            exit_qty, exit_px = _avg_fill([o for o in same if o.direction != side])
            if entry_qty != qty or exit_qty != qty:
                # same strike traded twice today, or a fill we can't account for
                _log_verify(
                    f"{r['position_id']}:{symbol}",
                    f"result=SKIPPED reason=leg_qty_differs position={r['position_id']} "
                    f"symbol={symbol} db_qty={qty} live_entry_qty={entry_qty} "
                    f"live_exit_qty={exit_qty}",
                )
                break
            fills.append((leg, entry_px, exit_px, qty))
        if len(fills) != len(all_legs) or not fills:
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
    return corrected


async def reconcile_closed_fills(pool: Any, broker: Any, broker_name: str = "kite") -> list[str]:
    """Correct today's closed live rows to the broker's fills; returns what changed.

    Idempotent: a second run finds nothing to correct, and sends no message.
    """
    orders = [
        o for o in await broker.get_all_orders() if o.status == "FILLED" and o.filled_quantity
    ]
    corrected = await _sweep_equity(pool, orders, broker_name)
    corrected += await _sweep_options(pool, orders, broker_name)
    if corrected:
        _LOG.warning("closed_fill_sweep_corrected count=%d %s", len(corrected), corrected)
        try:
            notify(
                EventKind.STRATEGY_ERROR,
                is_paper=False,
                segment=Segment.INTRADAY,
                dedupe_key=f"closed_fill_sweep:{'|'.join(corrected)}",
                title="PRICES CORRECTED FROM BROKER",
                strategy=broker_name,
                reason="\n".join(corrected)[:800],
            )
        except Exception:  # noqa: BLE001 -- notify() must never fail the sweep
            pass
    return corrected
