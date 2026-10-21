"""
Paper broker adapter — simulates order execution using real market data.

In-memory state only. Uses a real BrokerInterface (KiteAdapter) for
market data (LTP, OHLCV, instruments). Orders and positions are local.

Usage:
  real = KiteAdapter.from_env()
  paper = PaperAdapter(market_data_broker=real, initial_capital=200_000.0)
"""

from __future__ import annotations

import itertools
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.brokers.adapters.paper.fill_simulator import check_fill, check_gtt_triggers
from src.brokers.base import (
    BrokerError,
    BrokerInterface,
    Fill,
    GTTOrder,
    GTTRequest,
    Holding,
    Instrument,
    Margin,
    MarketDepth,
    OptionQuote,
    Order,
    OrderMarginResult,
    OrderRequest,
    OrderWindowClosed,
    Position,
    SessionInfo,
    SessionStatus,
    Tick,
)
from src.infrastructure.logger import get_logger
from src.market import sessions

if TYPE_CHECKING:
    from src.market_data.router import MarketDataRouter

# "src.brokers" -> brokers.log (logger.py _COMPONENTS); paper fills share that file
# with the live Kite/Angel One adapters since it's the same broker-call surface.
_log = get_logger("src.brokers.paper")
_IST = ZoneInfo("Asia/Kolkata")
_ORDER_ID_PREFIX = "PAPER"
_COUNTER = itertools.count(1)
_DERIV_PREFIXES = ("NFO:", "BFO:")  # derivative symbols whose recent prices are cached


class PaperAdapter(BrokerInterface):
    """
    Paper trading adapter.

    All order execution is simulated locally.
    Market data is fetched from the injected real broker.
    GTT orders are checked against LTP on get_all_gtts().
    """

    def __init__(
        self,
        market_data_broker: BrokerInterface | MarketDataRouter,
        initial_capital: float = 200_000.0,
        slippage_bps: float = 5.0,
        enforce_order_windows: bool = False,
    ) -> None:
        # True in production (brokers/factory.py): paper then rejects orders in the
        # no-order gaps exactly like the real broker (config/market_hours.json), so a
        # 15:47 signal can't sit SUBMITTED for 37 min as on 2026-09-21. Off by default
        # so unit tests don't depend on the wall clock.
        self._enforce_order_windows = enforce_order_windows
        self._market = market_data_broker
        self._slippage_bps = slippage_bps
        self._cash = initial_capital
        self._orders: dict[str, Order] = {}
        self._fills: dict[str, list[Fill]] = {}
        # Original request per order — needed to re-evaluate a resting order against a
        # fresh LTP in get_order_status(). Without it the fill decision made once in
        # place_order() is the only one the order ever gets (see 2026-08-25).
        self._requests: dict[str, OrderRequest] = {}
        self._gtts: dict[str, GTTOrder] = {}
        self._positions: dict[str, Position] = {}  # keyed by f"{symbol}:{product}"
        # Recent NFO/BFO prices ("NFO:SYM" -> (monotonic ts, price)). The option order path was queueing
        # behind every other loop's Kite quote calls (1 req/s shared bucket): ~12 s PER price lookup,
        # ~80 s for one two-leg entry on 2026-10-12. Entry/stop code has just fetched these prices a
        # moment earlier, so reuse them for a few seconds. NFO only: equity paths are untouched.
        self._nfo_px: dict[str, tuple[float, float]] = {}

    _NFO_PX_TTL_SECONDS = 5.0

    @staticmethod
    def _ltp_key(order: OrderRequest) -> str:
        """Exchange-qualified key for an order's price lookup. A bare option symbol used to reach
        Kite as "NSE:<option>", which does not exist, so every paper option order stayed unfilled."""
        if order.exchange in (None, "", "NSE"):
            return (
                order.symbol
            )  # equity: unchanged behaviour (the Kite adapter prefixes NSE: itself)
        return f"{order.exchange}:{order.symbol}"

    def _remember_nfo(self, prices: dict[str, float]) -> None:
        now = time.monotonic()
        for key, px in prices.items():
            if key.startswith(_DERIV_PREFIXES) and px:
                self._nfo_px[key] = (now, float(px))

    def _fresh_nfo(self, key: str) -> float | None:
        hit = self._nfo_px.get(key)
        if hit is not None and time.monotonic() - hit[0] <= self._NFO_PX_TTL_SECONDS:
            return hit[1]
        return None

    async def _order_ltp(self, order: OrderRequest) -> float:
        key = self._ltp_key(order)
        cached = self._fresh_nfo(key)
        if cached is not None:
            return cached
        return float((await self._market.get_ltp([key])).get(key, 0.0) or 0.0)

    def _next_order_id(self) -> str:
        return f"{_ORDER_ID_PREFIX}-{next(_COUNTER):06d}"

    def _next_gtt_id(self) -> str:
        return f"PAPER-GTT-{next(_COUNTER):06d}"

    def _update_position(
        self, symbol: str, exchange: str, product: str, fill: Fill, direction: str
    ) -> None:
        key = f"{symbol}:{product}"
        pos = self._positions.get(key)
        qty = fill.quantity
        price = fill.price

        if direction == "BUY":
            if pos is None:
                self._positions[key] = Position(
                    symbol=symbol,
                    exchange=exchange,
                    direction="long",
                    quantity=qty,
                    average_price=price,
                    last_price=price,
                    pnl=0.0,
                    product=product,
                )
            else:
                total_qty = pos.quantity + qty
                avg_price = (pos.average_price * pos.quantity + price * qty) / total_qty
                self._positions[key] = Position(
                    symbol=symbol,
                    exchange=exchange,
                    direction="long",
                    quantity=total_qty,
                    average_price=avg_price,
                    last_price=price,
                    pnl=0.0,
                    product=product,
                )
        else:  # SELL
            if pos is not None:
                new_qty = pos.quantity - qty
                if new_qty <= 0:
                    del self._positions[key]
                else:
                    self._positions[key] = Position(
                        symbol=symbol,
                        exchange=exchange,
                        direction=pos.direction,
                        quantity=new_qty,
                        average_price=pos.average_price,
                        last_price=price,
                        pnl=0.0,
                        product=product,
                    )

    # ── Authentication (no-op for paper trading) ───────────────────────────────

    async def login(self) -> SessionInfo:
        return SessionInfo(access_token="PAPER", user_id="PAPER", login_time=datetime.now(_IST))  # noqa: S106

    async def refresh_token(self) -> SessionInfo:
        return await self.login()

    async def get_session_status(self) -> SessionStatus:
        return SessionStatus(is_valid=True, user_id="PAPER", message="paper mode")

    # ── Market Data (delegated to real broker) ─────────────────────────────────

    async def subscribe_ticks(self, tokens: list[str]) -> None:
        await self._market.subscribe_ticks(tokens)

    async def unsubscribe_ticks(self, tokens: list[str]) -> None:
        await self._market.unsubscribe_ticks(tokens)

    def hint_prices(self, prices: dict[str, float]) -> None:
        """Give simulated fills the quotes a decision was JUST made on (entry: the chain mids; stop/exit:
        the polling tick's LTPs), so a paper order never waits on a Kite round trip. Real brokers have no
        such method, so real orders are unaffected."""
        self._remember_nfo(prices)

    async def priority_ltp(self, symbols: list[str]) -> dict[str, float]:
        """get_ltp for time-critical callers: uses the market broker's dedicated lane when it has one."""
        fn = getattr(self._market, "priority_ltp", None)
        if fn is None:
            return await self.get_ltp(symbols)
        prices: dict[str, float] = await fn(symbols)
        self._remember_nfo(prices)
        return prices

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        if symbols and all(
            s.startswith(_DERIV_PREFIXES) and self._fresh_nfo(s) is not None for s in symbols
        ):
            return {s: self._nfo_px[s][1] for s in symbols}
        prices = await self._market.get_ltp(symbols)
        self._remember_nfo(prices)
        return prices

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        return await self._market.get_ohlcv(symbol, interval, days)

    async def get_ohlcv_with_source(
        self, symbol: str, interval: str, days: int
    ) -> tuple[pd.DataFrame, str | None]:
        """Bars plus the provider that served them (None when the market source is untagged)."""
        fn = getattr(self._market, "get_ohlcv_with_source", None)
        if fn is not None:
            return await fn(symbol, interval, days)  # type: ignore[no-any-return]
        return await self._market.get_ohlcv(symbol, interval, days), None

    async def load_instrument_tokens(self, exchange: str = "NSE") -> int:
        # _refresh_intraday_ohlcv pre-warms the token cache via
        # getattr(broker, "load_instrument_tokens"). Without this delegate the paper
        # wrapper had no such attribute, the pre-warm silently never ran, and every
        # symbol's first fetch after a restart went through Kite's 1 req/s quote()
        # lookup: ~500 symbols = the ~640s first cycle (later cycles ~165s).
        loader = getattr(self._market, "load_instrument_tokens", None)
        return int(await loader(exchange)) if loader is not None else 0

    async def get_depth(self, symbol: str) -> MarketDepth:
        return await self._market.get_depth(symbol)

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        return await self._market.get_instruments(exchange)

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        quotes = await self._market.get_option_quotes(symbols)
        self._remember_nfo({k: q.ltp for k, q in quotes.items()})
        return quotes

    def set_access_token(self, token: str) -> None:
        """Paper orders are simulated, but market data still comes from the
        real broker underneath — refresh that one when the token rotates."""
        # A router has no broadcast set_access_token (tokens are broker-specific). This is
        # only ever called with a Kite token by the Kite refresh loop; Angel One has its
        # own refresh path, so target the router's kite provider explicitly.
        set_provider_token = getattr(self._market, "set_provider_token", None)
        if set_provider_token is not None:
            set_provider_token("kite", token)
            return
        set_token = getattr(self._market, "set_access_token", None)
        if set_token is not None:
            set_token(token)

    # ── Orders ─────────────────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> str:
        now = datetime.now(_IST)
        if self._enforce_order_windows and sessions.order_variety_at(now) == sessions.CLOSED:
            raise OrderWindowClosed(
                f"no order window open at {now:%H:%M} IST (see config/market_hours.json)"
            )
        order_id = self._next_order_id()
        _log.debug(
            "paper_place_order_received order_id=%s symbol=%s direction=%s qty=%s "
            "order_type=%s product=%s price=%s",
            order_id,
            order.symbol,
            order.direction,
            order.quantity,
            order.order_type,
            order.product,
            order.price,
        )

        ltp = await self._order_ltp(order)
        # No live LTP (e.g. an AMO/CNC order placed after the market closes, or a
        # feed hiccup) — leave the order resting OPEN instead of faking a fill off
        # the last daily close. A real broker fills an AMO at the NEXT session's
        # open, not off a stale same-day print, and get_order_status() already
        # re-evaluates OPEN orders against a fresh LTP on every poll, so the order
        # fills for real once the next session's live price is available (found
        # 2026-10-08: CHENNPETRO "filled" at today's 1605 open via this fallback
        # while the AMO was placed at 16:00, after the stock had already fallen to
        # 1529 intraday — a fictitious same-day fill, not a real trade).
        filled, fill = (
            (False, None) if not ltp else check_fill(order, ltp, order_id, self._slippage_bps)
        )

        if filled and fill is not None:
            self._fills[order_id] = [fill]
            self._update_position(
                order.symbol, order.exchange, order.product, fill, order.direction
            )
            status, filled_qty, pending_qty, avg_price = "FILLED", order.quantity, 0, fill.price
            _log.debug(
                "paper_place_order_found order_id=%s ltp=%.2f action=FILLED fill_price=%.2f",
                order_id,
                ltp,
                fill.price,
            )
        else:
            self._fills[order_id] = []
            status, filled_qty, pending_qty, avg_price = "OPEN", 0, order.quantity, 0.0
            _log.debug(
                "paper_place_order_found order_id=%s ltp=%.2f action=OPEN_unfilled", order_id, ltp
            )

        self._orders[order_id] = Order(
            broker_order_id=order_id,
            symbol=order.symbol,
            direction=order.direction,
            quantity=order.quantity,
            filled_quantity=filled_qty,
            pending_quantity=pending_qty,
            status=status,
            order_type=order.order_type,
            product=order.product,
            average_price=avg_price,
            tag=order.tag,
            placed_at=now,
            fills=list(self._fills[order_id]),
        )
        self._requests[order_id] = order
        return order_id

    async def modify_order(self, order_id: str, params: dict[str, Any]) -> str:
        if order_id not in self._orders:
            raise KeyError(f"Paper order {order_id!r} not found")
        order = self._orders[order_id]
        if order.status not in ("OPEN", "PARTIAL"):
            raise ValueError(f"Cannot modify order in status {order.status!r}")
        self._orders[order_id] = Order(
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            direction=order.direction,
            quantity=int(params.get("quantity", order.quantity)),
            filled_quantity=order.filled_quantity,
            pending_quantity=order.pending_quantity,
            status=order.status,
            order_type=order.order_type,
            product=order.product,
            average_price=order.average_price,
            tag=order.tag,
            placed_at=order.placed_at,
            fills=order.fills,
        )
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        if order_id not in self._orders:
            raise KeyError(f"Paper order {order_id!r} not found")
        order = self._orders[order_id]
        if order.status in ("FILLED", "CANCELLED", "REJECTED"):
            return False
        self._orders[order_id] = Order(
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            direction=order.direction,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            pending_quantity=0,
            status="CANCELLED",
            order_type=order.order_type,
            product=order.product,
            average_price=order.average_price,
            tag=order.tag,
            placed_at=order.placed_at,
            fills=order.fills,
        )
        return True

    async def get_order_status(self, order_id: str) -> Order:
        """Current status of a paper order, re-evaluating resting orders against LTP.

        A real broker keeps a resting order live and fills it the moment price becomes
        marketable. The simulator used to decide the fill exactly once inside
        place_order() and then replay that frozen record forever, so an entry that
        missed by a tick stayed OPEN for the whole session while the DB row sat at
        SUBMITTED and nothing ever revisited it (2026-08-25: AFFLE/BLS/BRIGADE/CEATLTD).
        Terminal orders are returned untouched — re-evaluation must never resurrect one.
        """
        if order_id not in self._orders:
            raise KeyError(f"Paper order {order_id!r} not found")
        order = self._orders[order_id]
        if order.status not in ("OPEN", "PARTIAL"):
            return order
        req = self._requests.get(order_id)
        if req is None:
            return order
        try:
            ltp = await self._order_ltp(req)
        except Exception:
            return order  # feed hiccup — leave it resting, next cycle retries
        if not ltp:
            return order
        filled, fill = check_fill(req, ltp, order_id, self._slippage_bps)
        if not filled or fill is None:
            return order
        self._fills[order_id] = [fill]
        self._update_position(req.symbol, req.exchange, req.product, fill, req.direction)
        self._orders[order_id] = Order(
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            direction=order.direction,
            quantity=order.quantity,
            filled_quantity=order.quantity,
            pending_quantity=0,
            status="FILLED",
            order_type=order.order_type,
            product=order.product,
            average_price=fill.price,
            tag=order.tag,
            placed_at=order.placed_at,
            fills=[fill],
        )
        return self._orders[order_id]

    async def get_all_orders(self) -> list[Order]:
        return list(self._orders.values())

    async def get_trades(self, order_id: str) -> list[Fill]:
        return list(self._fills.get(order_id, []))

    async def get_order_margin(self, orders: list[OrderRequest]) -> OrderMarginResult:
        # Margin calc is a market fact (what would this basket cost right now),
        # identical in kind to an LTP/quote -- delegate to the real Kite session,
        # same as get_ltp/get_option_quotes above. Unlike get_margins (funds),
        # this is never simulated locally: a paper sleeve's cash balance is
        # account state we must fake, but real margin requirement is not ours
        # to invent.
        return await self._market.get_order_margin(orders)

    # ── Positions & Portfolio ──────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_holdings(self) -> list[Holding]:
        return []

    async def get_margins(self) -> Margin:
        return Margin(equity=self._cash, available_cash=self._cash, used_margin=0.0)

    # ── GTT Orders ─────────────────────────────────────────────────────────────

    async def place_gtt(self, gtt: GTTRequest) -> str:
        gtt_id = self._next_gtt_id()
        self._gtts[gtt_id] = GTTOrder(
            gtt_id=gtt_id,
            symbol=gtt.symbol,
            status="ACTIVE",
            gtt_type=gtt.gtt_type,
            sl_trigger=gtt.sl_trigger,
            target_trigger=gtt.target_trigger,
        )
        return gtt_id

    async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str:
        """Move a simulated GTT's trigger level(s).

        Until 2026-08-28 this returned success WITHOUT touching the stored triggers,
        so a trailed stop kept firing off its original level while the caller logged
        a successful trail — the simulation reported work it had not done. Mirrors
        the Kite adapter's params contract: ``sl_trigger`` and ``target_trigger``.
        """
        if gtt_id not in self._gtts:
            raise KeyError(f"Paper GTT {gtt_id!r} not found")
        gtt = self._gtts[gtt_id]
        _sl = params.get("sl_trigger")
        _tgt = params.get("target_trigger")
        self._gtts[gtt_id] = GTTOrder(
            gtt_id=gtt.gtt_id,
            symbol=gtt.symbol,
            status=gtt.status,
            gtt_type=gtt.gtt_type,
            sl_trigger=float(_sl) if _sl is not None else gtt.sl_trigger,
            target_trigger=float(_tgt) if _tgt is not None else gtt.target_trigger,
        )
        return gtt_id

    async def cancel_gtt(self, gtt_id: str) -> bool:
        if gtt_id not in self._gtts:
            raise KeyError(f"Paper GTT {gtt_id!r} not found")
        gtt = self._gtts[gtt_id]
        self._gtts[gtt_id] = GTTOrder(
            gtt_id=gtt.gtt_id,
            symbol=gtt.symbol,
            status="CANCELLED",
            gtt_type=gtt.gtt_type,
            sl_trigger=gtt.sl_trigger,
            target_trigger=gtt.target_trigger,
        )
        return True

    async def get_all_gtts(self) -> list[GTTOrder]:
        """Return GTTs, auto-triggering active ones if LTP crosses their levels."""
        active = [g for g in self._gtts.values() if g.status == "ACTIVE"]
        if active:
            symbols = list({g.symbol for g in active})
            ltp_map = await self.get_ltp(symbols)
            for gtt in active:
                ltp = ltp_map.get(gtt.symbol, 0.0)
                leg = check_gtt_triggers(gtt.sl_trigger, gtt.target_trigger, ltp)
                if leg:
                    self._gtts[gtt.gtt_id] = GTTOrder(
                        gtt_id=gtt.gtt_id,
                        symbol=gtt.symbol,
                        status="TRIGGERED",
                        gtt_type=gtt.gtt_type,
                        sl_trigger=gtt.sl_trigger,
                        target_trigger=gtt.target_trigger,
                    )
        return list(self._gtts.values())

    # ── Capability flags ───────────────────────────────────────────────────────

    def supports_gtt(self) -> bool:
        return True

    def supports_bracket_order(self) -> bool:
        return True

    def supports_cover_order(self) -> bool:
        return True

    # ── Normalization (delegated to real broker) ───────────────────────────────

    def normalize_tick(self, raw: dict[str, Any]) -> Tick:
        return self._market.normalize_tick(raw)

    def normalize_order(self, raw: dict[str, Any]) -> Order:
        return self._market.normalize_order(raw)

    def normalize_position(self, raw: dict[str, Any]) -> Position:
        return self._market.normalize_position(raw)

    def normalize_error(self, raw: dict[str, Any]) -> BrokerError:
        return self._market.normalize_error(raw)
