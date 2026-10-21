"""
Kite broker adapter — implements BrokerInterface using kiteconnect SDK.

All public methods translate canonical models to Kite API calls and back.
No business logic here — only protocol translation.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect

from src.brokers.adapters.kite import normalizer
from src.brokers.adapters.kite.auth import KiteAuth
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
from src.brokers.rate_limiter import RateLimiter
from src.infrastructure.logger import get_logger
from src.market import sessions

# NOT "brokers.kite" (a flat name orphaned from logger.py's _COMPONENTS hierarchy,
# which maps "src.brokers" -> brokers.log) -- "src.brokers.kite" correctly nests
# under it. The prior flat name meant every warning below only ever reached
# root/console, never brokers.log.
_log = get_logger("src.brokers.kite")
_IST = ZoneInfo("Asia/Kolkata")

# Canonical product → Kite product code
_PRODUCT_TO_KITE: dict[str, str] = {
    "INTRADAY": "MIS",
    "DELIVERY": "CNC",
    "MARGIN": "NRML",
}

# Canonical GTT type → Kite GTT type string
_GTT_TYPE_TO_KITE: dict[str, str] = {
    "SINGLE_LEG": KiteConnect.GTT_TYPE_SINGLE,
    "TWO_LEG": KiteConnect.GTT_TYPE_OCO,  # Kite calls two-leg "OCO"
}

_GTT_STATUS_MAP: dict[str, str] = {
    "active": "ACTIVE",
    "triggered": "TRIGGERED",
    "disabled": "CANCELLED",
    "expired": "EXPIRED",
    "cancelled": "CANCELLED",
    "deleted": "CANCELLED",
    "rejected": "CANCELLED",
}

with (Path(__file__).parent.parent.parent / "capability_matrix.json").open() as _f:
    _CAPS: dict[str, Any] = json.load(_f).get("kite", {})


def _round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Snap a price to the exchange tick grid.

    Kite rejects any GTT/SL trigger or limit price that isn't an exact multiple
    of the instrument's tick size (0.05 for virtually all NSE equities) with
    "Stoploss trigger price should be a multiple of tick size 0.05." — every
    caller here computes SL/target from raw ATR/percentage math, which almost
    never lands on a clean multiple, so this must run right before the price
    leaves the process.
    """
    return round(round(price / tick_size) * tick_size, 2)


def _to_instrument(raw: dict[str, Any]) -> Instrument:
    expiry_val = raw.get("expiry")
    expiry: datetime | None = None
    if expiry_val and str(expiry_val).strip():
        if isinstance(expiry_val, datetime):
            expiry = expiry_val
        else:
            try:
                expiry = datetime.strptime(str(expiry_val)[:10], "%Y-%m-%d")
            except ValueError:
                expiry = None

    return Instrument(
        symbol=str(raw["tradingsymbol"]),
        exchange=str(raw["exchange"]),
        instrument_token=str(raw["instrument_token"]),
        instrument_type=str(raw.get("instrument_type", "EQ")),
        lot_size=int(raw.get("lot_size", 1)),
        tick_size=float(raw.get("tick_size", 0.05)),
        expiry=expiry,
        strike=float(raw["strike"]) if raw.get("strike") else None,
        name=str(raw.get("name", "")),
    )


def _to_gtt_order(raw: dict[str, Any]) -> GTTOrder:
    condition: dict[str, Any] = raw.get("condition", {})
    trigger_values: list[float] = condition.get("trigger_values", [0.0])
    gtt_type_raw = str(raw.get("type", "single")).lower()
    gtt_type = "TWO_LEG" if gtt_type_raw in {"two-leg", "oco"} else "SINGLE_LEG"

    sl_trigger = float(trigger_values[0]) if trigger_values else 0.0
    target_trigger = float(trigger_values[1]) if len(trigger_values) > 1 else None

    kite_status = str(raw.get("status", "active")).lower()
    canonical_status = _GTT_STATUS_MAP.get(kite_status, "ACTIVE")

    return GTTOrder(
        gtt_id=str(raw["id"]),
        symbol=str(condition.get("tradingsymbol", "")),
        status=canonical_status,
        gtt_type=gtt_type,
        sl_trigger=sl_trigger,
        target_trigger=target_trigger,
        raw=raw,
    )


def _margin_from_kite(data: dict[str, Any]) -> Margin:
    """Map a Kite margins payload to Margin.

    available_cash is the FREE CASH balance, deliberately excluding collateral:
    pledged holdings give intraday/F&O margin but cannot buy CNC delivery stock.
    Zerodha commonly reports available.cash as 0 and carries the real figure in
    live_balance (seen live 2026-08-27: cash 0, live_balance 29785.30, collateral
    557857.99, net 587643.29), so cash-then-live_balance is the honest read and
    `net` must never be used as spending power for delivery.
    """
    equity: dict[str, Any] = data.get("equity", {})
    available: dict[str, Any] = equity.get("available", {})
    cash = float(available.get("cash", 0.0) or 0.0)
    if not cash:
        cash = float(available.get("live_balance", 0.0) or 0.0)
    return Margin(
        equity=float(equity.get("net", 0.0)),
        available_cash=cash,
        used_margin=float(equity.get("utilised", {}).get("debits", 0.0)),
        collateral=float(available.get("collateral", 0.0)),
    )


def _order_margin_from_kite(data: dict[str, Any]) -> OrderMarginResult:
    """Map a Kite basket_order_margins payload to OrderMarginResult.

    VERIFIED against a live call 2026-10-12 (two NIFTY weekly short legs): the reply is
    ``{"initial": {..., "total": 189981.22}, "final": {..., "total": 184839.72},
    "orders": [{..., "total": 160708.5}, {..., "total": 29272.7}], "charges": {...}}``.
    ``initial`` is the margin before the basket's hedge/netting benefit, ``final`` is after it --
    ``final.total`` is the netted basket figure this class promises (see OrderMarginResult), and
    ``orders[i].total`` are the stand-alone per-leg figures (diagnostic only, never summed).
    The older shapes (a bare ``total``, or a list of per-leg ``total``) are still accepted. Anything
    else fails loudly rather than guessing a wrong total."""
    payload = data.get("data", data)
    if isinstance(payload, dict):
        final = payload.get("final")
        if isinstance(final, dict) and "total" in final:
            per_leg = [
                float(o["total"])
                for o in payload.get("orders", [])
                if isinstance(o, dict) and "total" in o
            ]
            return OrderMarginResult(total_margin=float(final["total"]), per_leg=per_leg, raw=data)
        if "total" in payload:
            return OrderMarginResult(total_margin=float(payload["total"]), raw=data)
    if isinstance(payload, list) and payload and "total" in payload[0]:
        legs = [float(item["total"]) for item in payload]
        return OrderMarginResult(total_margin=sum(legs), per_leg=legs, raw=data)
    raise ValueError(f"unrecognized order-margin response shape: keys={list(data.keys())}")


class KiteAdapter(BrokerInterface):
    """
    Kite broker adapter.

    All data translation goes through src/brokers/adapters/kite/normalizer.py.
    This class only calls the kiteconnect SDK and maps inputs/outputs.

    WebSocket tick subscriptions (subscribe_ticks / unsubscribe_ticks) record
    the requested tokens; actual KiteTicker lifecycle is managed by the
    supervisor / tick ingester, which calls kite.kws() separately.
    """

    def __init__(self, auth: KiteAuth, rate_limiter: RateLimiter | None = None) -> None:
        self._auth = auth
        self._limiter = rate_limiter if rate_limiter is not None else RateLimiter.for_kite()
        self._subscribed_tokens: set[str] = set()
        # tradingsymbol (bare, no exchange prefix) -> instrument_token. Populated
        # by load_instrument_tokens() so get_ohlcv() can skip its per-symbol
        # quote() call, which is otherwise the only way it resolves a token.
        self._token_cache: dict[str, int] = {}
        self._token_cache_lock = asyncio.Lock()
        # Market-data calls run on their OWN pool. They used the process-wide default
        # executor (12 workers here) shared with LLM, news, order and margin calls, and
        # measured 11.45s per historical call in-app against 0.06-0.9s raw from Kite
        # (2026-09-21). Sized to the 9-wide fetch cap (equity.screener.
        # max_concurrent_fetches) plus a few LTP/quote/instrument callers.
        self._io_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="kite-io")
        self._slow_call_logs = 0

    async def _io(self, fn: Any, *args: Any) -> Any:
        """Run a blocking Kite SDK call on the dedicated pool."""
        return await asyncio.get_running_loop().run_in_executor(
            self._io_pool, functools.partial(fn, *args)
        )

    @classmethod
    def from_env(cls) -> KiteAdapter:
        """Construct from KITE_* environment variables."""
        return cls(KiteAuth.from_env())

    @property
    def _kite(self) -> KiteConnect:
        return self._auth.kite

    # ── Authentication ─────────────────────────────────────────────────────────

    async def login(self) -> SessionInfo:
        return await self._auth.login()

    async def refresh_token(self) -> SessionInfo:
        return await self._auth.refresh_token()

    async def get_session_status(self) -> SessionStatus:
        return await self._auth.get_session_status()

    def set_access_token(self, token: str) -> None:
        """Update the live KiteConnect client's token in place — lets a
        long-running process pick up a refreshed token without restarting."""
        self._auth.set_access_token(token)

    # ── Market Data ────────────────────────────────────────────────────────────

    async def subscribe_ticks(self, tokens: list[str]) -> None:
        """Record tokens; KiteTicker subscribes when it next connects."""
        self._subscribed_tokens.update(tokens)

    async def unsubscribe_ticks(self, tokens: list[str]) -> None:
        self._subscribed_tokens.difference_update(tokens)

    async def priority_ltp(self, symbols: list[str]) -> dict[str, float]:
        """get_ltp on the dedicated time-critical lane (never queued behind scans)."""
        return await self._ltp(symbols, "quote_priority")

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return await self._ltp(symbols, "quote")

    async def _ltp(self, symbols: list[str], category: str) -> dict[str, float]:
        await self._limiter.acquire(category)
        # Kite's ltp() requires exchange-prefixed identifiers ("NSE:INFY").
        # A bare symbol ("INFY") yields an empty response. Accept either form:
        # prefix bare symbols for the request, then map results back to the
        # exact keys the caller passed so every call site works unchanged.
        query = [s if ":" in s else f"NSE:{s}" for s in symbols]
        orig_by_query = dict(zip(query, symbols, strict=False))
        # Bounded like get_ohlcv/quote below: this sits on the intraday scan loop's
        # first stage (_refresh_daily_counters -> daily_risk_counters MTM), so an
        # unbounded call here silently freezes every intraday strategy.
        raw: dict[str, Any] = await asyncio.wait_for(self._io(self._kite.ltp, query), timeout=10.0)
        return {orig_by_query.get(k, k): float(v["last_price"]) for k, v in raw.items()}

    async def get_quote(self, symbols: list[str]) -> dict[str, Any]:
        """Kite quote() rows (last_price, ohlc with the previous session's close, ...) keyed
        exactly as passed; bare symbols are queried on NSE like get_ltp. Read-only, used by
        the dashboard for day-change figures via MarketDataRouter.get_quote."""
        await self._limiter.acquire("quote")
        query = [s if ":" in s else f"NSE:{s}" for s in symbols]
        orig_by_query = dict(zip(query, symbols, strict=False))
        raw: dict[str, Any] = await asyncio.wait_for(
            self._io(self._kite.quote, query), timeout=10.0
        )
        return {orig_by_query.get(k, k): v for k, v in raw.items()}

    async def load_instrument_tokens(self, exchange: str = "NSE") -> int:
        """Bulk-load {tradingsymbol: instrument_token} for `exchange` from Kite's
        instrument dump, so get_ohlcv() can skip its per-symbol quote() lookup.

        Found 2026-09-15: the intraday scan's watchlist refresh (~500 symbols)
        was taking ~34 minutes per 5-minute cycle. get_ohlcv() re-resolved each
        symbol's instrument_token — which is static all session — via its own
        quote() call, serially, against Kite's documented 1 request/sec quote
        cap: ~500 sequential quote() calls alone floor a cycle at ~8+ minutes,
        and the measured per-symbol latency accounted for the rest.

        Idempotent: a non-empty cache is left alone (cheap to call every cycle).
        Never raises — a failed/partial dump degrades to an empty cache, and
        get_ohlcv()'s existing per-symbol quote() fallback still resolves any
        symbol not found here, so this is a pure optimization, not a dependency.
        """
        if self._token_cache:
            return len(self._token_cache)
        async with self._token_cache_lock:
            if self._token_cache:  # populated while we awaited the lock
                return len(self._token_cache)
            try:
                # Same underlying Kite call as get_instruments() below — reuse its
                # rate-limit bucket ("historical", not "quote": this is a contract
                # dump, not a live price lookup).
                await self._limiter.acquire("historical")
                # 2026-09-21: had no timeout, same gap as get_ohlcv below. Worse
                # here -- this is called ONCE, first, before ANY per-symbol
                # fetch in _refresh_intraday_ohlcv even starts, and the cache
                # is only skipped on a NON-empty result, so a single hang on
                # this call blocked every symbol, every cycle, forever (this
                # is the actual root cause behind today's stuck intraday scan
                # loop, not the per-symbol concurrency alone).
                dump: list[dict[str, Any]] = await asyncio.wait_for(
                    self._io(self._kite.instruments, exchange), timeout=15.0
                )
            except Exception as exc:
                # Never raises (see docstring) -- but a failure here silently
                # dropped every symbol back to the slow per-symbol quote()
                # fallback with zero trace anywhere (2026-09-21). Log it so
                # that degraded path is at least visible, not just inferred
                # from a slow cycle days later.
                _log.warning("load_instrument_tokens_failed exchange=%s err=%s", exchange, exc)
                return 0
            for row in dump:
                sym = row.get("tradingsymbol")
                token = row.get("instrument_token")
                if sym and token:
                    self._token_cache[str(sym)] = int(token)
            return len(self._token_cache)

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        """Fetch OHLCV history. Returns DataFrame with columns: date,open,high,low,close,volume."""
        # Look up the (static) instrument_token from the bulk cache first — see
        # load_instrument_tokens(). Only a cache miss (dump never loaded, or this
        # symbol isn't in it) falls through to the original per-symbol quote().
        token = self._token_cache.get(symbol.split(":")[-1])
        if token is None:
            # Two separate Kite endpoints, two separate documented rate caps
            # (quote=1/sec, historical=3/sec) — each call must acquire its OWN
            # bucket. Found 2026-09-08: this only acquired "historical" before
            # both calls, so the quote() sub-call below ran unthrottled against
            # its real 1/sec limit under concurrent batch downloads, got silently
            # throttled back with an empty response, and consistently starved the
            # same handful of symbols (HEG/HFCL/JBCHEPHARM) every night — looked
            # like "no quote data for this symbol" but was really "too many
            # quote() calls per second across the whole batch".
            await self._limiter.acquire("quote")
            quote: dict[str, Any] = await asyncio.wait_for(
                self._io(self._kite.quote, [symbol]), timeout=10.0
            )
            # Kite's quote() response key doesn't always match the requested key
            # exactly (same class of mismatch get_ltp above already guards against)
            # — fall back to whatever single entry IS present (a single-symbol
            # request has at most one) instead of assuming an exact match, and
            # raise a clear error instead of a bare, undiagnosable KeyError when
            # Kite genuinely returned nothing for this symbol (found 2026-09-08:
            # every "fetch failed SYM TF 'NSE:SYM'" log line was this exact
            # KeyError str()'d, with no indication of which case it was).
            row = quote.get(symbol) or next(iter(quote.values()), None)
            if row is None:
                raise ValueError(f"no quote data returned by Kite for {symbol!r}")
            token = int(row["instrument_token"])
            self._token_cache[symbol.split(":")[-1]] = token

        to_dt = datetime.now(_IST)
        from_dt = to_dt - timedelta(days=days)

        _t_req = time.monotonic()
        await self._limiter.acquire("historical")
        _t_acq = time.monotonic()
        _mark: dict[str, float] = {}

        def _timed_historical(*a: Any) -> Any:
            _mark["start"] = time.monotonic()
            try:
                return self._kite.historical_data(*a)
            finally:
                _mark["end"] = time.monotonic()

        # 2026-09-21: neither this nor any other asyncio.to_thread(self._kite...)
        # call in this adapter had a timeout -- a single hung/slow network call
        # (no response, dropped connection) blocked forever, and since callers
        # bound concurrency with a semaphore (e.g. _refresh_intraday_ohlcv), a
        # handful of hung requests eventually starved the whole batch (measured:
        # an intraday scan cycle stalled indefinitely instead of the intended
        # ~5 min). 10s matches this codebase's existing convention for external
        # API timeouts (config/global.json llm.gemini/anthropic timeout_seconds).
        # Scoped to get_ohlcv only for now -- the same gap exists on every other
        # to_thread(self._kite...) call in this file and is a separate, broader
        # fix.
        try:
            records: list[dict[str, Any]] = await asyncio.wait_for(
                self._io(
                    _timed_historical,
                    token,
                    from_dt.strftime("%Y-%m-%d"),
                    to_dt.strftime("%Y-%m-%d"),
                    interval,
                ),
                timeout=10.0,
            )
            _t_done = time.monotonic()
            if _t_done - _t_req > 5.0 and self._slow_call_logs < 10:
                self._slow_call_logs += 1
                _log.warning(
                    "kite_historical_slow sym=%s total=%.1fs limiter_wait=%.1fs "
                    "pool_queue=%.1fs kite_call=%.1fs loop_resume_lag=%.1fs",
                    symbol,
                    _t_done - _t_req,
                    _t_acq - _t_req,
                    _mark.get("start", _t_acq) - _t_acq,
                    _mark.get("end", 0.0) - _mark.get("start", 0.0),
                    _t_done - _mark.get("end", _t_done),
                )
        except TimeoutError as exc:
            # BrokerError (src/brokers/base.py) is a plain @dataclass value object
            # normalize_error() RETURNS, not an Exception subclass -- raising it
            # directly would itself crash with "exceptions must derive from
            # BaseException" at this line, masking the real timeout behind an
            # unrelated Python runtime error. Re-raise a real TimeoutError instead.
            raise TimeoutError(f"historical_data timed out for {symbol!r} after 10s") from exc
        return pd.DataFrame(records)

    async def get_depth(self, symbol: str) -> MarketDepth:
        await self._limiter.acquire("quote")
        raw: dict[str, Any] = await asyncio.to_thread(self._kite.quote, [symbol])
        return normalizer.normalize_depth(symbol, raw[symbol])

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        await self._limiter.acquire("historical")
        raw: list[dict[str, Any]] = await asyncio.to_thread(self._kite.instruments, exchange)
        return [_to_instrument(r) for r in raw]

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        if not symbols:
            return {}
        await self._limiter.acquire("quote")
        raw: dict[str, Any] = await asyncio.to_thread(self._kite.quote, symbols)
        return {sym: normalizer.normalize_option_quote(sym, raw[sym]) for sym in raw}

    # ── Orders ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _current_order_variety(strict: bool = True) -> str:
        """REGULAR during the live session, AMO in the after-market window,
        per config/market_hours.json (src/market/sessions.py — the only place
        those times are defined; weekends are AMO).

        In the no-order gaps between the two (e.g. 15:30-16:00 — Zerodha
        rejects both varieties there, confirmed live 2026-09-17 and again as
        a stranded paper order 2026-09-21) `strict` raises OrderWindowClosed
        so nothing is submitted. Cancels pass strict=False: the order being
        cancelled was placed under whichever variety was open before the gap,
        so after the close it is REGULAR and before the open it is AMO."""
        now = datetime.now(_IST)
        variety = sessions.order_variety_at(now)
        if variety == sessions.REGULAR:
            return str(KiteConnect.VARIETY_REGULAR)
        if variety == sessions.AMO:
            return str(KiteConnect.VARIETY_AMO)
        if strict:
            raise OrderWindowClosed(
                f"no order window open at {now:%H:%M} IST (see config/market_hours.json)"
            )
        return str(
            KiteConnect.VARIETY_REGULAR
            if now.time() >= sessions.regular_close()
            else KiteConnect.VARIETY_AMO
        )

    async def place_order(self, order: OrderRequest) -> str:
        await self._limiter.acquire("order")
        kite_product = _PRODUCT_TO_KITE.get(order.product, order.product)
        variety = self._current_order_variety()
        _log.debug(
            "kite_place_order_received symbol=%s exchange=%s direction=%s qty=%s "
            "product=%s(%s) order_type=%s price=%s variety=%s",
            order.symbol,
            order.exchange,
            order.direction,
            order.quantity,
            order.product,
            kite_product,
            order.order_type,
            order.price,
            variety,
        )
        order_id = await asyncio.to_thread(
            self._kite.place_order,
            variety=variety,
            exchange=order.exchange,
            tradingsymbol=order.symbol,
            transaction_type=order.direction,
            quantity=order.quantity,
            product=kite_product,
            order_type=order.order_type,
            validity=order.validity,
            price=order.price,
            trigger_price=order.trigger_price,
            tag=order.tag or order.agent_trade_id[:16],
        )
        _log.debug("kite_place_order_found symbol=%s broker_order_id=%s", order.symbol, order_id)
        return str(order_id)

    async def modify_order(self, order_id: str, params: dict[str, Any]) -> str:
        await self._limiter.acquire("order")
        _log.debug("kite_modify_order_received order_id=%s params=%s", order_id, params)
        result = await asyncio.to_thread(
            self._kite.modify_order,
            variety=self._current_order_variety(),
            order_id=order_id,
            **params,
        )
        _log.debug("kite_modify_order_found order_id=%s result=%s", order_id, result)
        return str(result)

    async def cancel_order(self, order_id: str) -> bool:
        await self._limiter.acquire("order")
        _log.debug("kite_cancel_order_received order_id=%s", order_id)
        await asyncio.to_thread(
            self._kite.cancel_order,
            variety=self._current_order_variety(strict=False),
            order_id=order_id,
        )
        _log.debug("kite_cancel_order_found order_id=%s action=cancelled", order_id)
        return True

    async def get_order_status(self, order_id: str) -> Order:
        await self._limiter.acquire("general")
        orders: list[dict[str, Any]] = await asyncio.to_thread(self._kite.orders)
        for raw in orders:
            if str(raw.get("order_id")) == order_id:
                normalized = normalizer.normalize_order(raw)
                _log.debug(
                    "kite_get_order_status_found order_id=%s status=%s", order_id, normalized.status
                )
                return normalized
        _log.debug("kite_get_order_status_found order_id=%s status=NOT_FOUND", order_id)
        raise KeyError(f"Order {order_id!r} not found in today's order book")

    async def get_all_orders(self) -> list[Order]:
        await self._limiter.acquire("general")
        raw_orders: list[dict[str, Any]] = await asyncio.to_thread(self._kite.orders)
        return [normalizer.normalize_order(r) for r in raw_orders]

    async def get_trades(self, order_id: str) -> list[Fill]:
        await self._limiter.acquire("general")
        raw_trades: list[dict[str, Any]] = await asyncio.to_thread(
            self._kite.order_trades, order_id
        )
        return [
            Fill(
                order_id=str(t["order_id"]),
                trade_id=str(t["trade_id"]),
                quantity=int(t["quantity"]),
                price=float(t["average_price"]),
                exchange_time=normalizer._parse_ts(t.get("exchange_timestamp")),  # noqa: SLF001
            )
            for t in raw_trades
        ]

    # ── Positions & Portfolio ──────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        await self._limiter.acquire("general")
        data: dict[str, Any] = await asyncio.to_thread(self._kite.positions)
        net: list[dict[str, Any]] = data.get("net", [])
        return [normalizer.normalize_position(r) for r in net]

    async def get_holdings(self) -> list[Holding]:
        await self._limiter.acquire("general")
        raw_holdings: list[dict[str, Any]] = await asyncio.to_thread(self._kite.holdings)
        return [
            Holding(
                symbol=str(h["tradingsymbol"]),
                exchange=str(h.get("exchange", "NSE")),
                quantity=int(h["quantity"]),
                average_price=float(h["average_price"]),
                last_price=float(h["last_price"]),
                pnl=float(h.get("pnl", 0.0)),
                t1_quantity=int(h.get("t1_quantity", 0)),
            )
            for h in raw_holdings
        ]

    async def get_margins(self) -> Margin:
        await self._limiter.acquire("general")
        data: dict[str, Any] = await asyncio.to_thread(self._kite.margins)
        return _margin_from_kite(data)

    async def get_order_margin(self, orders: list[OrderRequest]) -> OrderMarginResult:
        await self._limiter.acquire("general")
        variety = self._current_order_variety(strict=False)
        params = [
            {
                "exchange": o.exchange,
                "tradingsymbol": o.symbol,
                "transaction_type": o.direction,
                "variety": variety,
                "product": _PRODUCT_TO_KITE.get(o.product, o.product),
                "order_type": o.order_type,
                "quantity": o.quantity,
                "price": o.price or 0.0,
                "trigger_price": o.trigger_price or 0.0,
            }
            for o in orders
        ]
        data: dict[str, Any] = await asyncio.to_thread(
            self._kite.basket_order_margins, params, consider_positions=False
        )
        return _order_margin_from_kite(data)

    # ── GTT Orders ─────────────────────────────────────────────────────────────

    async def place_gtt(self, gtt: GTTRequest) -> str:
        # Zerodha's GTT API accepts LIMIT legs only (kite.trade/docs/connect/v3/gtt).
        # A MARKET leg comes back as the opaque "Invalid trigger data." with the
        # position left unprotected, so refuse it here where the cause is obvious.
        if gtt.sl_order_type.upper() != "LIMIT":
            raise ValueError(
                f"GTT leg order_type must be LIMIT for Kite, got {gtt.sl_order_type!r} "
                "(see order_types.CNC_swing_gtt_sl_leg in config/execution.json)"
            )
        await self._limiter.acquire("order")
        kite_product = _PRODUCT_TO_KITE.get(gtt.product, gtt.product)
        kite_type = _GTT_TYPE_TO_KITE.get(gtt.gtt_type, KiteConnect.GTT_TYPE_SINGLE)

        ltp_map = await self.get_ltp([gtt.symbol])
        last_price = ltp_map.get(gtt.symbol, 0.0)

        trigger_values: list[float]
        orders: list[dict[str, Any]]

        sl_trigger = _round_to_tick(gtt.sl_trigger)
        sl_price = _round_to_tick(gtt.sl_price if gtt.sl_price is not None else gtt.sl_trigger)

        if gtt.gtt_type == "TWO_LEG" and gtt.target_trigger is not None:
            target_trigger = _round_to_tick(gtt.target_trigger)
            target_price = _round_to_tick(
                gtt.target_price if gtt.target_price is not None else gtt.target_trigger
            )
            trigger_values = [sl_trigger, target_trigger]
            orders = [
                {
                    "transaction_type": "SELL",
                    "quantity": gtt.quantity,
                    "product": kite_product,
                    "order_type": gtt.sl_order_type,
                    "price": sl_price,
                },
                {
                    "transaction_type": "SELL",
                    "quantity": gtt.quantity,
                    "product": kite_product,
                    "order_type": "LIMIT",
                    "price": target_price,
                },
            ]
        else:
            trigger_values = [sl_trigger]
            orders = [
                {
                    "transaction_type": "SELL",
                    "quantity": gtt.quantity,
                    "product": kite_product,
                    "order_type": gtt.sl_order_type,
                    "price": sl_price,
                }
            ]

        result: dict[str, Any] = await asyncio.to_thread(
            self._kite.place_gtt,
            trigger_type=kite_type,
            tradingsymbol=gtt.symbol,
            exchange="NSE",
            trigger_values=trigger_values,
            last_price=last_price,
            orders=orders,
        )
        return str(result["trigger_id"])

    async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str:
        """Move a live GTT's trigger level(s).

        Kite's modify_gtt takes the COMPLETE spec — trigger_type, tradingsymbol,
        exchange, trigger_values, last_price and orders are all mandatory positional
        arguments, so forwarding only the changed field (what this did until
        2026-08-28) raised TypeError before the call ever reached the API: every
        swing SL trail failed silently into update_sl's except branch. The current
        GTT is re-fetched and merged here instead, keeping the broker-agnostic
        `params` contract its callers use.

        Recognized params: ``sl_trigger`` (leg 0), ``target_trigger`` (leg 1) and
        ``quantity`` (applied to every leg, used after a partial book so the GTT does
        not rest for more stock than is still held).
        """
        await self._limiter.acquire("general")
        raw_gtts: list[dict[str, Any]] = await asyncio.to_thread(self._kite.get_gtts)
        current = next((g for g in raw_gtts if str(g.get("id")) == str(gtt_id)), None)
        if current is None:
            raise KeyError(f"GTT {gtt_id!r} not found at broker")

        condition: dict[str, Any] = current.get("condition", {})
        orders: list[dict[str, Any]] = [dict(o) for o in current.get("orders", [])]
        triggers: list[float] = [float(v) for v in condition.get("trigger_values", [])]

        for idx, key in ((0, "sl_trigger"), (1, "target_trigger")):
            if params.get(key) is None or idx >= len(triggers):
                continue
            new_value = _round_to_tick(float(params[key]))
            triggers[idx] = new_value
            # A resting leg's limit price tracks its own trigger — leaving it at the
            # old level would trigger at the trailed stop but fill at the entry stop.
            if idx < len(orders):
                orders[idx]["price"] = new_value

        if params.get("quantity") is not None:
            for o in orders:
                o["quantity"] = int(params["quantity"])

        symbol = str(condition.get("tradingsymbol", ""))
        exchange = str(condition.get("exchange", "NSE"))
        ltp_map = await self.get_ltp([symbol])
        last_price = ltp_map.get(symbol) or float(condition.get("last_price", 0.0))

        await self._limiter.acquire("order")
        result: dict[str, Any] = await asyncio.to_thread(
            self._kite.modify_gtt,
            trigger_id=int(gtt_id),
            trigger_type=str(current.get("type", KiteConnect.GTT_TYPE_SINGLE)),
            tradingsymbol=symbol,
            exchange=exchange,
            trigger_values=triggers,
            last_price=last_price,
            orders=orders,
        )
        return str(result.get("trigger_id", gtt_id))

    async def cancel_gtt(self, gtt_id: str) -> bool:
        await self._limiter.acquire("order")
        await asyncio.to_thread(self._kite.delete_gtt, trigger_id=int(gtt_id))
        return True

    async def get_all_gtts(self) -> list[GTTOrder]:
        await self._limiter.acquire("general")
        raw_gtts: list[dict[str, Any]] = await asyncio.to_thread(self._kite.get_gtts)
        return [_to_gtt_order(r) for r in raw_gtts]

    # ── Capability flags ───────────────────────────────────────────────────────

    def supports_gtt(self) -> bool:
        return bool(_CAPS.get("gtt_native", True))

    def supports_bracket_order(self) -> bool:
        return bool(_CAPS.get("bracket_order", True))

    def supports_cover_order(self) -> bool:
        return bool(_CAPS.get("cover_order", True))

    # ── Normalization ──────────────────────────────────────────────────────────

    def normalize_tick(self, raw: dict[str, Any]) -> Tick:
        return normalizer.normalize_tick(raw)

    def normalize_order(self, raw: dict[str, Any]) -> Order:
        return normalizer.normalize_order(raw)

    def normalize_position(self, raw: dict[str, Any]) -> Position:
        return normalizer.normalize_position(raw)

    def normalize_error(self, raw: dict[str, Any]) -> BrokerError:
        return normalizer.normalize_error(raw)
