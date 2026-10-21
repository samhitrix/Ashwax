"""
PaperTradingEngine — orchestrates paper trade simulation.

Per docs/05-order-lifecycle.md §6.5:
  - Routes to paper_trades table (NOT trades table)
  - Uses REAL broker LTP for fills and P&L
  - Runs parallel to live trade monitoring (live trades NEVER abandoned)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface

from src.notifications import EventKind, notify
from src.notifications.events import Segment
from src.paper_trading.fill_simulator import PaperFillSimulator
from src.paper_trading.gtt_simulator import PaperGTT, PaperGTTSimulator
from src.paper_trading.paper_reconciler import PaperReconciler
from src.paper_trading.position_tracker import PaperPosition, PaperPositionTracker

_LOG = logging.getLogger("src.paper_trading.engine")


class PaperTradingEngine:
    """Full paper trading simulation engine. Uses real LTP from broker."""

    def __init__(self, broker: BrokerInterface, pool: Any = None) -> None:
        self._broker = broker
        self._pool = pool
        self._tracker = PaperPositionTracker()
        self._fill_sim = PaperFillSimulator(broker)
        self._gtt_sim = PaperGTTSimulator()
        self._reconciler = PaperReconciler(self._tracker, pool)

    async def place_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        strategy_name: str,
        intended_price: float,
        initial_sl: float,
        initial_target: float,
        product: str = "DELIVERY",
        regime_at_entry: str | None = None,
        setup_quality_score: float | None = None,
    ) -> str | None:
        """Simulate a paper order. Returns agent_trade_id on fill, None on failure."""
        from src.brokers.base import OrderRequest

        agent_trade_id = f"PAPER-{uuid.uuid4().hex[:12].upper()}"
        req = OrderRequest(
            symbol=symbol,
            exchange="NSE",
            direction=direction,
            quantity=quantity,
            order_type="MARKET",
            product=product,
            validity="DAY",
            agent_trade_id=agent_trade_id,
            signal_id="",
            tag=strategy_name[:20],
            price=intended_price,
        )

        fill = await self._fill_sim.try_fill(req)
        if not fill.filled:
            _LOG.warning("paper_fill_failed symbol=%s", symbol)
            return None

        # Re-anchor SL/target to the ACTUAL fill (preserving R:R). The signal
        # levels were computed off the breakout reference; a MARKET order fills
        # at the live LTP, which can sit past the signal target → arming the
        # stale levels fires the GTT instantly (the TARGET_HIT-at-a-loss bug).
        from src.execution.post_fill_risk import reanchor_protection

        _ra = reanchor_protection(
            fill_price=fill.fill_price,
            signal_entry=intended_price,
            signal_sl=initial_sl,
            signal_target=initial_target,
            direction=direction,
        )
        if _ra.degenerate:
            _LOG.warning("paper_reanchor_skipped symbol=%s %s", symbol, _ra.message)
        else:
            initial_sl, initial_target = _ra.sl, _ra.target
            _LOG.info("paper_reanchor symbol=%s %s", symbol, _ra.message)

        pos = PaperPosition(
            agent_trade_id=agent_trade_id,
            symbol=symbol,
            direction=direction,
            quantity=quantity,
            fill_price=fill.fill_price,
            initial_sl=initial_sl,
            current_sl=initial_sl,
            initial_target=initial_target,
            current_target=initial_target,
            strategy_name=strategy_name,
        )
        self._tracker.open(pos)
        self._gtt_sim.arm(
            PaperGTT(
                agent_trade_id=agent_trade_id,
                symbol=symbol,
                sl_trigger=initial_sl,
                target_trigger=initial_target,
                quantity=quantity,
                direction=direction,
            )
        )
        await self._persist_open(
            pos,
            fill.fill_price,
            fill.simulated_slippage_pct,
            intended_price,
            regime_at_entry,
            setup_quality_score,
        )
        _LOG.info("paper_order_filled symbol=%s fill=%.2f", symbol, fill.fill_price)
        return agent_trade_id

    async def place_options_structure(
        self, choice: Any, signal_id: str, strategy_name: str = "options"
    ) -> str | None:
        """Simulate a multi-leg options structure (Phase 6).

        Each leg fills at its quoted premium (real OI/IV upstream, premium used as
        the leg LTP here). Long legs get a premium-based SL + target armed so the
        paper monitor can close them; short legs are covered by construction.
        Returns the structure id, or None if no leg filled.
        """
        from src.brokers.base import OrderRequest

        structure_id = f"PAPER-OPT-{uuid.uuid4().hex[:12].upper()}"
        opened = 0
        for leg in choice.legs:
            exch, sym = (
                leg.tradingsymbol.split(":", 1)
                if ":" in leg.tradingsymbol
                else ("NFO", leg.tradingsymbol)
            )
            leg_id = f"{structure_id}-{leg.instrument_type}-{int(leg.strike)}"
            req = OrderRequest(
                symbol=leg.tradingsymbol,
                exchange=exch,
                direction=leg.direction,
                quantity=leg.quantity,
                order_type="MARKET",
                product="MARGIN",
                validity="DAY",
                agent_trade_id=leg_id,
                signal_id=signal_id,
                tag=strategy_name[:20],
                price=leg.premium,
            )
            # Fill at the quoted premium (override LTP so no live feed is needed).
            fill = await self._fill_sim.try_fill(req, current_ltp=leg.premium)
            if not fill.filled:
                continue
            is_long = leg.direction == "BUY"
            sl = leg.premium * 0.5 if is_long else 0.0
            target = leg.premium * 2.0 if is_long else 0.0
            pos = PaperPosition(
                agent_trade_id=leg_id,
                symbol=leg.tradingsymbol,
                direction=leg.direction,
                quantity=leg.quantity,
                fill_price=fill.fill_price,
                initial_sl=sl,
                current_sl=sl,
                initial_target=target,
                current_target=target,
                strategy_name=strategy_name,
            )
            self._tracker.open(pos)
            if is_long:
                self._gtt_sim.arm(
                    PaperGTT(
                        agent_trade_id=leg_id,
                        symbol=leg.tradingsymbol,
                        sl_trigger=sl,
                        target_trigger=target,
                        quantity=leg.quantity,
                        direction=leg.direction,
                    )
                )
            await self._persist_open(pos, fill.fill_price, fill.simulated_slippage_pct, leg.premium)
            opened += 1

        if opened == 0:
            _LOG.warning("paper_options_no_fill id=%s", structure_id)
            return None
        _LOG.info(
            "paper_options_structure_placed id=%s type=%s legs=%d",
            structure_id,
            getattr(choice, "structure_type", "?"),
            opened,
        )
        return structure_id

    async def monitor_cycle(self) -> list[dict[str, Any]]:
        """One monitoring cycle — call every 2-3s. Returns closed trade events."""
        events: list[dict[str, Any]] = []
        symbols = self._tracker.symbols()
        if not symbols:
            return events

        try:
            ltp_map = await self._broker.get_ltp(symbols)
        except Exception:
            return events

        for sym in symbols:
            raw = ltp_map.get(sym) or ltp_map.get(f"NSE:{sym}")
            if isinstance(raw, dict):
                ltp = float(raw.get("last_price", 0) or 0)
            elif raw is not None:
                ltp = float(raw)
            else:
                ltp = 0.0
            if not ltp:
                continue

            self._tracker.update_ltp(sym, ltp)
            for t in self._gtt_sim.check(sym, ltp):
                pos = self._tracker.close(t.agent_trade_id)
                self._gtt_sim.disarm(t.agent_trade_id)
                if pos:
                    mult = 1 if pos.direction.upper() == "BUY" else -1
                    pnl = (t.fill_price - pos.fill_price) * pos.quantity * mult
                    events.append(
                        {
                            "type": t.trigger_type,
                            "agent_trade_id": t.agent_trade_id,
                            "symbol": sym,
                            "exit_price": t.fill_price,
                            "pnl": pnl,
                        }
                    )
                    await self._persist_close(t.agent_trade_id, t.fill_price, pnl, t.trigger_type)
                    _LOG.info("paper_closed symbol=%s %s pnl=%.2f", sym, t.trigger_type, pnl)

        return events

    @property
    def tracker(self) -> PaperPositionTracker:
        return self._tracker

    async def reconcile(self) -> None:
        result = await self._reconciler.reconcile()
        if not result.clean:
            _LOG.warning("paper_gaps count=%d", len(result.gaps))

    async def _eod_exit_price(
        self, ohlcv_repo: Any, symbol: str, entry_ts: datetime, fill_price: Any
    ) -> tuple[float, datetime]:
        """Resolve (exit_price, exit_ts) for an EOD square-off as the LAST 5m
        close on entry_ts's IST date. Fallback (no repo / no candle) is the fill
        price at entry_ts → the trade is scratched (realized P&L 0)."""
        fill = float(fill_price or 0)
        if ohlcv_repo is None:
            return fill, entry_ts
        try:
            rows = await ohlcv_repo.fetch_candles(symbol, "5m", 600)
        except Exception:
            rows = []
        ist = ZoneInfo("Asia/Kolkata")
        target = entry_ts.astimezone(ist).date()
        best: Any = None
        for r in rows:
            ts = r["ts"]
            if ts.astimezone(ist).date() == target and (best is None or ts > best["ts"]):
                best = r
        if best is None:
            return fill, entry_ts
        return float(best["close"]), best["ts"]

    async def daily_risk_counters(
        self,
        table: str = "paper_trades",
        *,
        product: str | None = None,
        exec_broker: str | None = None,
    ) -> dict[str, Any]:
        """Day-scoped CRM counters reconstructed from today's trades (IST).

        The CRMContext holds these in-memory, so a mid-day launcher restart would
        reset them to 0 and silently bypass the caps (root cause of 6-before +
        6-after = 12 trades on 2026-06-18). Sourcing them from the DB (the source of
        truth) keeps every counter correct for the whole trading day and across any
        restart. Returns: new_trades_today, open_position_count, daily_loss_so_far
        (positive rupees), consecutive_losses (trailing losing streak by exit time).

        `table` selects "paper_trades" (default) or "trades" — Phase 2 (per-segment
        concurrent paper/live) calls this once per active mode so live and paper get
        fully independent counters, never summed together. Restricted to this
        trusted, internally-controlled pair of literals, never external input.

        `product` (None/"MIS"/"CNC") scopes every query to one product -- "MIS"
        drives intraday's own daily-loss cap (config/capital.json intraday.max_loss_per_day),
        kept separate from swing's P&L since swing has NO daily loss limit by design
        and must never be affected by how intraday's day is going (2026-10-07). None
        (default) keeps the existing combined behaviour for callers not yet split.

        `exec_broker` (live `trades` only; per-broker risk, system.per_broker_equity) scopes
        every query to the rows that broker holds; a NULL stamp means "kite" (Phase 2 rule).
        None (default) = every live broker combined, exactly as before.
        """
        if table not in ("paper_trades", "trades"):
            raise ValueError(f"daily_risk_counters: unsupported table {table!r}")
        if product is not None and product not in ("MIS", "CNC"):
            raise ValueError(f"daily_risk_counters: unsupported product {product!r}")
        if exec_broker is not None and table != "trades":
            raise ValueError("daily_risk_counters: exec_broker applies to live trades only")
        _p = f" AND product = '{product}'" if product else ""
        _args: tuple[Any, ...] = ()
        if exec_broker is not None:
            _p += " AND COALESCE(exec_broker, 'kite') = $1"
            _args = (exec_broker,)
        counters: dict[str, Any] = {
            "new_trades_today": 0,
            "open_position_count": 0,
            "daily_loss_so_far": 0.0,
            "consecutive_losses": 0,
            "per_strategy": {},
            "open_per_strategy": {},
        }
        if self._pool is None:
            return counters
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT count(*) AS placed, "  # noqa: S608 — table/product are trusted literals, checked above
                f"  COALESCE(sum(pnl_realized) FILTER (WHERE order_state='CLOSED'), 0) AS net_pnl "
                f"FROM {table} "
                f"WHERE (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                f"= (NOW() AT TIME ZONE 'Asia/Kolkata')::date{_p}",
                *_args,
            )
            closed = await conn.fetch(
                f"SELECT pnl_realized FROM {table} "  # noqa: S608
                f"WHERE order_state='CLOSED' AND exit_timestamp IS NOT NULL "
                f"AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                f"= (NOW() AT TIME ZONE 'Asia/Kolkata')::date{_p} "
                f"ORDER BY exit_timestamp DESC",
                *_args,
            )
            per_strat_rows = await conn.fetch(
                f"SELECT strategy_name, count(*) AS n FROM {table} "  # noqa: S608
                f"WHERE (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                f"= (NOW() AT TIME ZONE 'Asia/Kolkata')::date{_p} "
                f"GROUP BY strategy_name",
                *_args,
            )
            # Open positions (any entry date, NOT just today) — a swing position can sit
            # open for days, so both the G7 open-position cap and the daily-loss breaker
            # must see EVERY currently open position, not only ones entered today (the
            # prior today-scoped query silently dropped carried-over positions from the
            # cap check, letting swing accumulate past max_open_positions unblocked).
            open_rows = await conn.fetch(
                f"SELECT symbol, direction, quantity, fill_price, strategy_name "  # noqa: S608
                f"FROM {table} "
                f"WHERE order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
                f"AND fill_price IS NOT NULL{_p}",
                *_args,
            )
        counters["new_trades_today"] = int(row["placed"] or 0)
        counters["open_position_count"] = len(open_rows)
        open_per_strategy: dict[str, int] = {}
        for r in open_rows:
            _sn = r["strategy_name"]
            if _sn:
                open_per_strategy[str(_sn)] = open_per_strategy.get(str(_sn), 0) + 1
        counters["open_per_strategy"] = open_per_strategy
        realized_net = float(row["net_pnl"] or 0.0)
        # Mark-to-market: fold UNREALIZED P&L on open positions into the day's loss so
        # the daily-loss cap sees money currently at risk, not only booked losses. The
        # realized-only cap could never stop a burst of simultaneous entries (they all
        # opened before any booked a loss). Broker LTP unavailable → fall back to
        # realized-only (never crash the risk refresh, never fabricate a price).
        unrealized_net = 0.0
        if open_rows:
            try:
                ltp_map = await self._broker.get_ltp([r["symbol"] for r in open_rows])
            except Exception:
                ltp_map = {}
            for r in open_rows:
                _ltp = float(ltp_map.get(r["symbol"], 0) or 0)
                _fill = float(r["fill_price"] or 0)
                if _ltp <= 0 or _fill <= 0:
                    continue
                _mult = 1 if str(r["direction"]).lower() in ("buy", "long") else -1
                unrealized_net += (_ltp - _fill) * int(r["quantity"]) * _mult
        counters["unrealized_pnl"] = unrealized_net
        # daily_loss_so_far is POSITIVE rupees of loss (realized + unrealized combined).
        counters["daily_loss_so_far"] = max(0.0, -(realized_net + unrealized_net))
        streak = 0
        for r in closed:
            if r["pnl_realized"] is not None and float(r["pnl_realized"]) < 0:
                streak += 1
            else:
                break
        counters["consecutive_losses"] = streak
        counters["per_strategy"] = {
            str(r["strategy_name"]): int(r["n"]) for r in per_strat_rows if r["strategy_name"]
        }
        return counters

    async def square_off_open(
        self,
        ohlcv_repo: Any = None,
        *,
        prior_day_only: bool = False,
        reason: str = "EOD_SQUAREOFF",
        price_mode: str = "eod_candle",
        order_monitors: list[Any] | None = None,
    ) -> int:
        """Force-close open INTRADAY (MIS) paper positions directly in the DB
        (not tracker-driven, so it also closes stale rows left over from a prior
        launcher run).

        order_monitors: every active stack's OrderMonitor (shared["stacks"]'s
        "order_monitor" values). Optional but should always be passed by real
        callers — without it, a closed position lingers in OrderMonitor's
        in-memory book and every consumer of get_monitored() (dashboard,
        periodic position-update notification) keeps showing it as open.

        CNC (swing/delivery) positions are never eligible here — they're meant to
        be held overnight/multi-day, unlike MIS which must flatten same-day. This
        sweep is the intraday exit-window/EOD backstop; swing exits are handled
        entirely by their own SL/target/CRM path (src/orchestrator.py
        _execute_swing_signal), never by a same-day flatten.

        prior_day_only=True → only rows whose IST entry date is before today
        (the startup sweep that clears yesterday's trades). Returns count closed.

        price_mode="ltp" prices each exit at the current broker LTP and stamps the
        exit at the flatten moment (now) — used by the hard exit-window flat-by so
        nothing is priced at the day's last 5m candle (15:25). Default "eod_candle"
        keeps last-candle pricing for the 15:35 EOD backstop.
        """
        from datetime import datetime

        if self._pool is None:
            return 0
        where = (
            "order_state NOT IN ('CLOSED','REJECTED','CANCELLED','TIMED_OUT') "
            "AND fill_price IS NOT NULL AND product = 'MIS'"
        )
        if prior_day_only:
            where += (
                " AND (entry_timestamp AT TIME ZONE 'Asia/Kolkata')::date "
                "< (NOW() AT TIME ZONE 'Asia/Kolkata')::date"
            )
        closed = 0
        async with self._pool.acquire() as conn:
            sql = f"SELECT agent_trade_id, symbol, direction, quantity, fill_price, entry_timestamp FROM paper_trades WHERE {where}"  # noqa: S608
            rows = await conn.fetch(sql)
            ltp_map: dict[str, float] = {}
            if price_mode == "ltp" and rows:
                try:
                    ltp_map = await self._broker.get_ltp([r["symbol"] for r in rows])
                except Exception:
                    ltp_map = {}
            for r in rows:
                _ltp = float(ltp_map.get(r["symbol"], 0) or 0) if price_mode == "ltp" else 0.0
                if _ltp > 0:
                    exit_price = _ltp
                    exit_ts = datetime.now(ZoneInfo("Asia/Kolkata"))
                else:
                    exit_price, exit_ts = await self._eod_exit_price(
                        ohlcv_repo, r["symbol"], r["entry_timestamp"], r["fill_price"]
                    )
                qty = int(r["quantity"])
                mult = 1 if str(r["direction"]).lower() in ("buy", "long") else -1
                pnl = (exit_price - float(r["fill_price"] or 0)) * qty * mult
                await conn.execute(
                    # $3 is the LAST leg (`quantity` is what remained after partials);
                    # pnl_realized is the whole trade, so add what partials already banked
                    # — same rule as TradeRepository.update_pnl.
                    "UPDATE paper_trades SET order_state='CLOSED', exit_price=$2, "
                    "pnl_realized=$3 + COALESCE(partial_pnl_booked, 0), "
                    "exit_timestamp=$4, exit_reason=$5, updated_at=NOW() "
                    "WHERE agent_trade_id=$1",
                    r["agent_trade_id"],
                    Decimal(str(round(exit_price, 2))),
                    Decimal(str(round(pnl, 2))),
                    exit_ts,
                    reason,
                )
                aid = r["agent_trade_id"]
                if self._tracker.get(aid) is not None:
                    self._tracker.close(aid)
                self._gtt_sim.disarm(aid)
                # Untrack from every live OrderMonitor too — without this, a
                # position closed here (DB + legacy tracker only) stays in
                # OrderMonitor._positions forever, so get_monitored() keeps
                # returning it and the periodic position-update notification
                # keeps reporting an already-closed trade as still open, all
                # day (found 2026-09-16). untrack() is a safe no-op if this
                # position was never tracked by a given monitor.
                for _om in order_monitors or []:
                    try:
                        _om.untrack(aid)
                    except Exception:
                        pass
                closed += 1
                try:
                    notify(
                        EventKind.EXIT,
                        is_paper=True,
                        agent_trade_id=aid,
                        symbol=r["symbol"],
                        # This sweep only ever selects product='MIS' rows (see the
                        # WHERE clause above) -- always intraday.
                        segment=Segment.INTRADAY,
                        dedupe_key=f"exit:{aid}",
                        reason=reason,
                        entry_price=float(r["fill_price"] or 0),
                        exit_price=exit_price,
                        quantity=qty,
                        net_pnl=pnl,
                    )
                except Exception:  # notify() must never fail the square-off
                    pass
        if closed:
            _LOG.info(
                "paper_square_off reason=%s closed=%d prior_day=%s",
                reason,
                closed,
                prior_day_only,
            )
        return closed

    async def _persist_open(
        self,
        pos: PaperPosition,
        fill_price: float,
        slippage: float,
        intended: float,
        regime_at_entry: str | None = None,
        setup_quality_score: float | None = None,
    ) -> None:
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO paper_trades
                      (agent_trade_id, symbol, direction, quantity, product, strategy_name,
                       intended_price, fill_price, slippage_pct, order_state,
                       initial_sl, current_sl, initial_target, current_target,
                       regime_at_entry, setup_quality_score,
                       is_paper, entry_timestamp, created_at, updated_at)
                    VALUES ($1,$2,$3,$4,'CNC',$5,$6,$7,$8,'FILLED',
                            $9,$9,$10,$10,$11,$12,true,NOW(),NOW(),NOW())
                    """,
                    pos.agent_trade_id,
                    pos.symbol,
                    "long" if pos.direction.upper() in ("BUY", "LONG") else "short",
                    pos.quantity,
                    pos.strategy_name,
                    Decimal(str(intended)),
                    Decimal(str(fill_price)),
                    Decimal(str(slippage)),
                    Decimal(str(pos.initial_sl)),
                    Decimal(str(pos.initial_target)),
                    regime_at_entry,
                    Decimal(str(setup_quality_score)) if setup_quality_score is not None else None,
                )
        except Exception as exc:
            _LOG.error("paper_persist_open err=%s", exc)

    async def _persist_close(
        self, agent_trade_id: str, exit_price: float, pnl: float, reason: str
    ) -> None:
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    # The tracker never shrinks on a partial, so the `pnl` the caller computed
                    # covers the ORIGINAL size at the exit price. The whole trade is: what remained
                    # x (exit - fill), plus what the partial already banked.
                    """
                    UPDATE paper_trades
                    SET order_state='CLOSED', exit_price=$2,
                        pnl_realized = (CASE WHEN lower(direction) IN ('buy', 'long') THEN 1 ELSE -1 END)
                                       * ($2 - fill_price) * quantity + COALESCE(partial_pnl_booked, 0),
                        exit_timestamp=NOW(), exit_reason=$3, updated_at=NOW()
                    WHERE agent_trade_id=$1
                    """,
                    agent_trade_id,
                    Decimal(str(exit_price)),
                    reason,
                )
        except Exception as exc:
            _LOG.error("paper_persist_close err=%s", exc)
