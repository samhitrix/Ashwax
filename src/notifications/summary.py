"""Periodic open-position rollup + end-of-day summary loops.

Both run as their own asyncio tasks inside the orchestrator's main gather
(orchestrator.py:9479), matching the existing loop idioms (_heartbeat_loop,
_equity_loop). Reads from shared["stacks"] (see orchestrator.py:_build_stack)
and TradeRepository — never touches OrderMonitor.cycle() or anything on the
trade-decision path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.brokers.stack_keys import PAPER_KEY, primary_live_key, stack_broker, stack_is_paper
from src.market import sessions
from src.market.calendar import holiday_name, is_holiday, is_trading_day
from src.notifications.config import load_notification_config
from src.notifications.events import EventKind, segment_for_product
from src.notifications.notifier import already_sent, get_notifier

if TYPE_CHECKING:
    from src.db.repositories.trades import TradeRepository

_LOG = logging.getLogger(__name__)  # "src.notifications.summary" -> notifications.log
_IST = ZoneInfo("Asia/Kolkata")


def _hhmm_to_minutes(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _now_minutes(now: datetime) -> int:
    return now.hour * 60 + now.minute


async def build_position_snapshot(stacks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per open position across both stacks, with a fresh LTP batched
    per-stack (one get_ltp call per broker, not per symbol)."""
    rows: list[dict[str, Any]] = []
    for stack_key, stack in stacks.items():
        is_paper = stack_is_paper(stack_key)
        order_monitor = stack.get("order_monitor")
        broker = stack.get("broker")
        if order_monitor is None:
            continue
        monitored = order_monitor.get_monitored()
        if not monitored:
            continue
        symbols = sorted({pos.symbol for pos in monitored})
        ltp_map: dict[str, float] = {}
        if broker is not None and symbols:
            try:
                ltp_map = await broker.get_ltp(symbols)
            except Exception as exc:
                _LOG.warning("position_snapshot_ltp_failed stack=%s err=%s", stack_key, exc)
        for pos in monitored:
            ltp = ltp_map.get(pos.symbol, pos.fill_price)
            direction_sign = 1.0 if pos.direction.upper() == "BUY" else -1.0
            unrealized = (ltp - pos.fill_price) * pos.quantity * direction_sign
            rows.append(
                {
                    "is_paper": is_paper,
                    # Which broker holds it (None for paper / the legacy bare "live" key);
                    # the formatter only shows it once live spans two or more brokers.
                    "broker": stack_broker(stack_key),
                    "segment": segment_for_product(pos.product).value,
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "fill_price": pos.fill_price,
                    "ltp": ltp,
                    "current_sl": pos.current_sl,
                    "current_target": pos.current_target,
                    "unrealized_pnl": unrealized,
                }
            )
    return rows


async def build_options_snapshot(seller_desk: Any, broker: Any) -> list[dict[str, Any]]:
    """One row per LEG of every open SellerDesk options position (not one
    aggregated row per structure) — a spread's protective and income legs
    move independently and showing only the netted total hides which leg is
    actually driving the P&L (found 2026-09-16: user could only see one row
    for a 2-leg bull_put_spread). Mark-to-market via a single batched
    get_ltp across every leg of every open position. position_id groups
    rows belonging to the same structure back together in the formatter."""
    positions = list(getattr(seller_desk, "positions", {}).values())
    if not positions:
        return []
    is_paper = bool(getattr(seller_desk, "_is_paper", True))
    all_legs = [(pos, leg) for pos in positions for leg in pos.protection_legs + pos.income_legs]
    symbols = sorted({leg.tradingsymbol for _pos, leg in all_legs})
    ltp_map: dict[str, float] = {}
    if broker is not None and symbols:
        try:
            ltp_map = await broker.get_ltp(symbols)
        except Exception as exc:
            _LOG.warning("options_snapshot_ltp_failed err=%s", exc)

    rows: list[dict[str, Any]] = []
    for pos, leg in all_legs:
        current = ltp_map.get(leg.tradingsymbol, leg.premium)
        # BUY (long) leg profits when premium rises; SELL (short) leg
        # profits when premium falls — same sign convention as
        # seller_desk._close_position's realized_pnl calculation.
        sign = 1.0 if leg.direction == "BUY" else -1.0
        leg_pnl = sign * (current - leg.premium) * leg.quantity
        rows.append(
            {
                "is_paper": is_paper,
                "segment": "OPTIONS",
                "position_id": pos.position_id,
                "symbol": pos.underlying,
                "structure_type": pos.strategy_type,
                "strike": leg.strike,
                "instrument_type": leg.instrument_type,
                "direction": leg.direction,
                "lots": leg.lots,
                "premium": leg.premium,
                "ltp": current,
                "unrealized_pnl": leg_pnl,
            }
        )
    return rows


async def collect_open_rows(shared: dict[str, Any]) -> list[dict[str, Any]]:
    """Open equity + options rows across stacks (shared by the 15-min update and EOD)."""
    stacks = shared.get("stacks") or {}
    rows = await build_position_snapshot(stacks)
    _desk = (shared.get("seller_options") or {}).get("desk")
    if _desk is not None:
        _opt_is_paper = bool(getattr(_desk, "_is_paper", True))
        # Options desk is single-broker: paper, else the primary live stack.
        _opt_key = (
            PAPER_KEY
            if _opt_is_paper
            else primary_live_key(stacks, shared.get("legacy_live_broker"))
        )
        _picked_stack = stacks.get(_opt_key) if _opt_key else None
        _opt_stack: dict[str, Any] = (
            _picked_stack if _picked_stack is not None else next(iter(stacks.values()), {})
        )
        rows = rows + await build_options_snapshot(_desk, _opt_stack.get("broker"))
    return rows


def _options_repo(shared: dict[str, Any]) -> Any:
    desk = (shared.get("seller_options") or {}).get("desk")
    return getattr(desk, "_repo", None)


async def position_update_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    cfg = load_notification_config()
    pu = cfg.position_update
    interval_s = max(60.0, pu.interval_minutes * 60.0)
    start_min = _hhmm_to_minutes(pu.start_hhmm)
    end_min = _hhmm_to_minutes(pu.end_hhmm)

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            break  # stop was set
        except TimeoutError:
            pass

        try:
            notifier = get_notifier()
            if notifier is None or not cfg.event_enabled("position_update"):
                continue
            now = datetime.now(_IST)
            if not (start_min <= _now_minutes(now) <= end_min):
                continue
            if not is_trading_day(now.date()):  # weekend or NSE holiday
                continue
            rows = await collect_open_rows(shared)

            if not rows and pu.skip_when_flat:
                continue

            realized_today = {True: 0.0, False: 0.0}
            trade_repo = shared.get("trade_repo")
            if trade_repo is not None:
                try:
                    realized_today = await realized_pnl_today_by_mode(
                        trade_repo, now.strftime("%Y-%m-%d"), _options_repo(shared)
                    )
                except Exception as exc:
                    _LOG.warning("position_update_realized_fetch_failed err=%s", exc)

            from src.notifications.notifier import notify

            notify(
                EventKind.POSITION_UPDATE,
                is_paper=True,
                rows=rows,
                realized_today_paper=realized_today[True],
                realized_today_live=realized_today[False],
            )
        except Exception as exc:
            _LOG.error("position_update_loop_error err=%s", exc)


async def _closed_trades_today(
    trade_repo: TradeRepository, date_str: str, is_paper: bool
) -> list[Any]:
    """Rows from `trade_repo` closed today (IST), for one mode. Shared by
    build_eod_stats and realized_pnl_today_by_mode — keeping the date-match
    logic in exactly one place is what stops the two from ever disagreeing
    the way daily_realized_pnl once silently did (see build_eod_stats'
    docstring: that method's own exit_timestamp::date SQL cast is
    timezone-fragile and is deliberately not used anywhere here)."""
    try:
        rows = await trade_repo.closed_trades(limit=500, is_paper=is_paper)
    except Exception as exc:
        _LOG.warning("closed_trades_today_failed is_paper=%s err=%s", is_paper, exc)
        return []
    out: list[Any] = []
    for row in rows:
        exit_ts = row.get("exit_timestamp")
        if exit_ts is None:
            continue
        row_date = (
            exit_ts.astimezone(_IST).strftime("%Y-%m-%d") if exit_ts.tzinfo else str(exit_ts)[:10]
        )
        if row_date == date_str:
            out.append(row)
    return out


async def _options_closed_today(
    options_repo: Any, date_str: str, is_paper: bool
) -> list[dict[str, Any]]:
    """Options positions closed today (IST) for one mode. Options realized P&L
    lives in options_positions, not trades/paper_trades, so the equity-only
    readers above never saw it (found 2026-10-05: +₹1,995 booked options
    profit reported as ₹0 in both the 15-min update and the EOD summary)."""
    if options_repo is None:
        return []
    try:
        return list(
            await options_repo.closed_positions_on(date.fromisoformat(date_str), is_paper=is_paper)
        )
    except Exception as exc:
        _LOG.warning("options_closed_today_failed is_paper=%s err=%s", is_paper, exc)
        return []


async def _options_open_legs_booked_today(
    options_repo: Any, date_str: str, is_paper: bool
) -> float:
    """Realised P&L of legs closed today inside positions that are STILL OPEN (a per-leg stop closes one leg
    and the structure carries on). It is booked money, so it belongs in "Booked (realized)" and the total --
    for every strategy, read from the saved legs, nothing strategy-specific."""
    if options_repo is None:
        return 0.0
    try:
        return float(
            await options_repo.closed_legs_pnl_on(date.fromisoformat(date_str), is_paper=is_paper)
        )
    except Exception as exc:
        _LOG.warning("options_open_legs_booked_failed is_paper=%s err=%s", is_paper, exc)
        return 0.0


async def realized_pnl_today_by_mode(
    trade_repo: TradeRepository, date_str: str, options_repo: Any = None
) -> dict[bool, float]:
    """Today's (IST) realized P&L (equity + options), split by is_paper — the
    realized half of the position update's combined "Total P/L" (today's
    realized + current unrealized), per user request."""
    out: dict[bool, float] = {True: 0.0, False: 0.0}
    for is_paper in (False, True):
        rows = await _closed_trades_today(trade_repo, date_str, is_paper)
        out[is_paper] = sum(float(r.get("pnl_realized") or 0) for r in rows)
        opt = await _options_closed_today(options_repo, date_str, is_paper)
        out[is_paper] += sum(float(r.get("realized_pnl") or 0) for r in opt)
        out[is_paper] += await _options_open_legs_booked_today(options_repo, date_str, is_paper)
    return out


async def build_eod_stats(
    trade_repo: TradeRepository, date_str: str, options_repo: Any = None
) -> dict[str, Any]:
    """Aggregate closed trades across BOTH trades and paper_trades for the
    given date. DB is source of truth (per feedback_source_of_truth).

    net_pnl is summed directly from the same `closed` rows used for
    trade_count/wins/losses/best/worst — NOT from TradeRepository.
    daily_realized_pnl(), which casts exit_timestamp::date using the DB
    session's default timezone (never set to IST anywhere in this codebase
    — see src/db/pool.py). That mismatch let a trade the IST-anchored
    filter below correctly counts get silently dropped by that other
    query's date match, producing a real "Trades: 1 (L 1)" alongside a
    wrong "Net P&L: ₹0.00" with a wrongly-green emoji (found 2026-09-15).
    Summing the already-filtered rows removes the second, disagreeing
    query entirely."""
    closed: list[Any] = []
    net_pnl = 0.0
    for is_paper in (False, True):
        rows = await _closed_trades_today(trade_repo, date_str, is_paper)
        closed.extend(rows)
        net_pnl += sum(float(r.get("pnl_realized") or 0) for r in rows)
    booked_equity = net_pnl

    # Options closes, normalised to the equity row shape so wins/losses/best/
    # worst below treat them identically. NULL realized_pnl = incomplete close:
    # counted as a trade (P&L unknown, treated as 0) and surfaced separately.
    booked_options = 0.0
    options_incomplete = 0
    for is_paper in (False, True):
        for r in await _options_closed_today(options_repo, date_str, is_paper):
            if r.get("realized_pnl") is None:
                options_incomplete += 1
            pnl = float(r.get("realized_pnl") or 0)
            booked_options += pnl
            closed.append({"symbol": r.get("underlying"), "pnl_realized": pnl})
    for is_paper in (False, True):
        booked_options += await _options_open_legs_booked_today(options_repo, date_str, is_paper)
    net_pnl += booked_options

    wins = [r for r in closed if float(r.get("pnl_realized") or 0) > 0]
    losses = [r for r in closed if float(r.get("pnl_realized") or 0) <= 0]
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0

    total_r = 0.0
    r_count = 0
    for r in closed:
        risk = float(r.get("actual_risk_rupees") or 0)
        pnl = float(r.get("pnl_realized") or 0)
        if risk > 0:
            total_r += pnl / risk
            r_count += 1
    net_pnl_r = total_r if r_count else 0.0

    best = max(closed, key=lambda r: float(r.get("pnl_realized") or 0), default=None)
    worst = min(closed, key=lambda r: float(r.get("pnl_realized") or 0), default=None)

    return {
        "trade_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "net_pnl_r": net_pnl_r,
        "booked_equity": booked_equity,
        "booked_options": booked_options,
        "options_incomplete": options_incomplete,
        "best_symbol": best.get("symbol") if best else "",
        "worst_symbol": worst.get("symbol") if worst else "",
    }


async def eod_summary_loop(shared: dict[str, Any], stop: asyncio.Event) -> None:
    cfg = load_notification_config()
    target_min = _hhmm_to_minutes(cfg.eod_hhmm)
    last_sent_date = ""

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
            break
        except TimeoutError:
            pass

        try:
            notifier = get_notifier()
            if notifier is None or not cfg.event_enabled("eod_summary"):
                continue
            now = datetime.now(_IST)
            if not is_trading_day(now.date()):  # weekend or NSE holiday
                continue
            date_str = now.strftime("%Y-%m-%d")
            if date_str == last_sent_date:
                continue
            if _now_minutes(now) < target_min:
                continue

            trade_repo = shared.get("trade_repo")
            if trade_repo is None:
                continue
            stats = await build_eod_stats(trade_repo, date_str, _options_repo(shared))
            open_rows = await collect_open_rows(shared)
            stats["open_count"] = len(open_rows)
            stats["open_unrealized"] = sum(float(r.get("unrealized_pnl") or 0) for r in open_rows)
            from src.notifications.notifier import notify

            notify(
                EventKind.EOD_SUMMARY,
                is_paper=True,
                dedupe_key=f"eod:{date_str}",
                **stats,
            )
            last_sent_date = date_str
        except Exception as exc:
            _LOG.error("eod_summary_loop_error err=%s", exc)


async def market_holiday_loop(stop: asyncio.Event) -> None:
    """Send the single 'NSE holiday' message on a weekday holiday; weekends send nothing.

    Waits out the notifier's startup quiet window first (events submitted inside it are
    dropped), and checks the persisted dedupe key so a restart never repeats the message.
    """
    cfg = load_notification_config()
    notice_at = sessions.schedule_time("market_holiday_notice")
    last_sent_date = ""

    try:
        await asyncio.wait_for(stop.wait(), timeout=cfg.throttle.startup_quiet_seconds + 5.0)
        return
    except TimeoutError:
        pass

    while not stop.is_set():
        try:
            now = datetime.now(_IST)
            today = now.date()
            date_str = today.isoformat()
            if (
                get_notifier() is not None
                and cfg.event_enabled("market_holiday")
                and date_str != last_sent_date
                and is_holiday(today)
                and today.weekday() < 5
                and now.time() >= notice_at
            ):
                key = f"holiday:{date_str}"
                if not await already_sent(key):
                    from src.notifications.notifier import notify

                    notify(
                        EventKind.MARKET_HOLIDAY,
                        is_paper=True,
                        dedupe_key=key,
                        name=holiday_name(today),
                    )
                last_sent_date = date_str
        except Exception as exc:
            _LOG.error("market_holiday_loop_error err=%s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30.0)
            break
        except TimeoutError:
            pass
