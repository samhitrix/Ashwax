"""
Angel One (SmartAPI) broker adapter — implements BrokerInterface using the
official `SmartApi` (smartapi-python) SDK.

All public methods translate canonical models to SmartAPI calls and back.
No business logic here — only protocol translation (same contract as
src/brokers/adapters/kite/adapter.py).

Route paths, request field names and response shapes below were verified
2026-09-22 against angel-one/smartapi-python's source (routes dict, method
bodies) and SmartAPI's published docs (Orders, Portfolio, User, Gtt,
MarketData, Historical, RateLimit). Two areas remain best-effort rather than
directly verified against a live account and are called out where they occur:
the full order-status enum, and the instrument master's tick_size/strike
scaling convention.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd

from src.brokers.adapters.angel_one import normalizer
from src.brokers.adapters.angel_one.auth import AngelOneAuth
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

# NOT "brokers.angel_one" (a flat name orphaned from logger.py's _COMPONENTS
# hierarchy, which maps "src.brokers" -> brokers.log, same bug found in the
# Kite adapter) -- "src.brokers.angel_one" correctly nests under it.
_log = get_logger("src.brokers.angel_one")
_IST = ZoneInfo("Asia/Kolkata")

# Angel One's public, unauthenticated instrument master dump (no SDK method
# fetches the full exchange instrument list — searchScrip() only returns
# matches for a search string). Verified 2026-09-22.
_INSTRUMENT_MASTER_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)

# Canonical product → Angel One producttype. Angel One's own strings already
# match the canonical vocabulary (verified against placeOrder/gttCreateRule
# examples using "INTRADAY" and "MARGIN" literally) — this is an identity map
# kept explicit for symmetry with the Kite adapter and easy future divergence.
# MIS / CNC are the Kite-style aliases some callers pass (order_lifecycle treats
# "MIS" as intraday). NRML is NOT aliased: whether Angel wants MARGIN or
# CARRYFORWARD for NFO carry-forward is UNVERIFIED (findings), so it fails at the
# broker rather than being guessed.
_PRODUCT_TO_ANGEL: dict[str, str] = {
    "INTRADAY": "INTRADAY",
    "DELIVERY": "DELIVERY",
    "MARGIN": "MARGIN",
    "MIS": "INTRADAY",
    "CNC": "DELIVERY",
}

# Canonical candle interval (the Kite Connect vocabulary this codebase's
# callers already pass — see src/market_data/ohlcv_downloader.py's
# `_TIMEFRAMES`/`kite_interval` and orchestrator.py's `kite_tf`, both used
# unchanged regardless of active broker) → SmartAPI's interval constant.
# Verified 2026-09-22 against smartapi.angelbroking.com/docs/Historical.
_INTERVAL_TO_ANGEL: dict[str, str] = {
    "minute": "ONE_MINUTE",
    "3minute": "THREE_MINUTE",
    "5minute": "FIVE_MINUTE",
    "10minute": "TEN_MINUTE",
    "15minute": "FIFTEEN_MINUTE",
    "30minute": "THIRTY_MINUTE",
    "60minute": "ONE_HOUR",
    "day": "ONE_DAY",
}

# Max span (days) of ONE getCandleData request per SmartAPI interval
# (docs/Historical "Max Days in one Request", findings B1). A longer lookback
# is fetched as several windows of at most (this - 1) days.
_MAX_DAYS_PER_REQUEST: dict[str, int] = {
    "ONE_MINUTE": 30,
    "THREE_MINUTE": 60,
    "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100,
    "FIFTEEN_MINUTE": 200,
    "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400,
    "ONE_DAY": 2000,
}

# Bare index names the codebase passes (nfo_chain's map etc.) that an AMXIDX row's
# `name` may alias; any other index is reachable by its symbol forms only.
_INDEX_BARE_NAMES = frozenset(
    {"NIFTY", "BANKNIFTY", "INDIAVIX", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
)

# getMarketData accepts up to 50 tokens per request (docs/MarketData, findings B1).
_MARKET_DATA_MAX_TOKENS = 50
# margin/v1/batch accepts up to 50 positions per request (findings B2).
_MARGIN_MAX_POSITIONS = 50
_OHLCV_COLUMNS = ["date", "open", "high", "low", "close", "volume"]
_DATE_DTYPE = "datetime64[ns, Asia/Kolkata]"

# Canonical order_type → (Angel One ordertype, Angel One variety override).
# SL/SL-M orders require variety="STOPLOSS" instead of the session-based
# NORMAL/AMO variety (verified via SmartAPI forum: "For STOPLOSS_LIMIT orders,
# set order_type='STOPLOSS_LIMIT', variety='STOPLOSS'"). STOPLOSS variety is
# assumed live-session-only (not AMO-eligible) — not directly verified against
# a real AMO-window STOPLOSS rejection; confirm before relying on SL/SL-M
# placement outside market hours.
_ORDER_TYPE_MAP: dict[str, tuple[str, str | None]] = {
    "MARKET": ("MARKET", None),
    "LIMIT": ("LIMIT", None),
    "SL": ("STOPLOSS_LIMIT", "STOPLOSS"),
    "SL-M": ("STOPLOSS_MARKET", "STOPLOSS"),
}

# SmartAPI GTT rule status (docs/Gtt ruleDetails: "status": "NEW") → canonical.
_GTT_STATUS_MAP: dict[str, str] = {
    "NEW": "ACTIVE",
    "TRIGGERED": "TRIGGERED",
    "CANCELLED": "CANCELLED",
    "EXPIRED": "EXPIRED",
}

with (Path(__file__).parent.parent.parent / "capability_matrix.json").open() as _f:
    _CAPS: dict[str, Any] = json.load(_f).get("angel_one", {})


class AngelOneMarginError(RuntimeError):
    """The margin batch API refused or returned an unusable response."""


def _round_to_tick(price: float, tick_size: float = 0.05) -> float:
    """Snap a price to the exchange tick grid (see kite/adapter.py's identical
    helper for the underlying NSE tick-size rationale — the same 0.05 grid
    applies regardless of broker)."""
    return round(round(price / tick_size) * tick_size, 2)


def _to_instrument(raw: dict[str, Any]) -> Instrument:
    """Translate one row of Angel One's OpenAPIScripMaster dump.

    tick_size and strike are commonly documented (SmartAPI community forum)
    as stored *100 (paise, not rupees) in this file — e.g. tick_size "5.00"
    means an actual 0.05 tick. Applied here but not directly verified against
    a live row; confirm against a known instrument (e.g. any NSE equity,
    which should show tick_size 0.05 after the /100) before trusting GTT/SL
    price rounding derived from it.
    """
    expiry_raw = str(raw.get("expiry") or "").strip()
    expiry: datetime | None = None
    if expiry_raw:
        try:
            expiry = datetime.strptime(expiry_raw, "%d%b%Y")
        except ValueError:
            expiry = None

    raw_tick = raw.get("tick_size")
    tick_size = 0.05 if raw_tick is None or raw_tick in ("", "0") else float(raw_tick) / 100.0
    # Non-option rows carry strike '-1.000000' (verified 2026-10-15, findings B5), so
    # "no strike" is any negative number, not the literal string "-1".
    strike: float | None
    try:
        strike_x100 = float(raw.get("strike"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        strike = None  # absent or '' -> no strike
    else:
        strike = None if strike_x100 < 0 else strike_x100 / 100.0

    return Instrument(
        symbol=str(raw["symbol"]),
        exchange=str(raw.get("exch_seg", "NSE")),
        instrument_token=str(raw["token"]),
        instrument_type=str(raw.get("instrumenttype") or "EQ"),
        lot_size=int(raw.get("lotsize") or 1),
        tick_size=tick_size,
        expiry=expiry,
        strike=strike,
        name=str(raw.get("name", "")),
    )


def _margin_from_angel(data: dict[str, Any]) -> Margin:
    """Map a SmartAPI getRMS payload to Margin. Field names verified
    2026-09-22 against docs/User's published example (net, availablecash,
    collateral, utiliseddebits — all top-level, unlike Kite's nested
    equity.available.* shape)."""
    return Margin(
        equity=float(data.get("net") or 0.0),
        available_cash=float(data.get("availablecash") or 0.0),
        used_margin=float(data.get("utiliseddebits") or 0.0),
        collateral=float(data.get("collateral") or 0.0),
    )


class AngelOneAdapter(BrokerInterface):
    """
    Angel One broker adapter.

    All data translation goes through src/brokers/adapters/angel_one/normalizer.py.
    This class only calls the SmartApi SDK and maps inputs/outputs.

    WebSocket tick subscriptions (subscribe_ticks/unsubscribe_ticks) record the
    requested tokens only; an actual SmartWebSocketV2 consumer is not wired up
    by this adapter — same deferred-ownership pattern as KiteAdapter, whose
    docstring notes the real websocket lifecycle belongs to a separate
    supervisor/ingester component.
    """

    def __init__(self, auth: AngelOneAuth, rate_limiter: RateLimiter | None = None) -> None:
        self._auth = auth
        self._limiter = rate_limiter if rate_limiter is not None else RateLimiter.for_angel_one()
        self._subscribed_tokens: set[str] = set()
        # exchange -> {tradingsymbol (bare, e.g. "SBIN-EQ"): (symboltoken, exch_seg)}.
        # Populated by load_instrument_tokens() — SmartAPI has no per-request
        # bulk quote call that accepts tradingsymbols directly; every order/
        # quote/candle call needs a resolved symboltoken first. Keyed per
        # exchange so loading NSE never blocks NFO (and the same tradingsymbol
        # on two exchanges keeps both tokens).
        self._token_cache: dict[str, dict[str, tuple[str, str]]] = {}
        self._token_cache_lock = asyncio.Lock()
        self._slow_call_logs = 0

    @classmethod
    def from_env(cls) -> AngelOneAdapter:
        """Construct from ANGEL_ONE_* environment variables."""
        return cls(AngelOneAuth.from_env())

    @property
    def _sc(self) -> Any:
        return self._auth.sc

    # ── Authentication ─────────────────────────────────────────────────────────

    async def login(self) -> SessionInfo:
        return await self._auth.login()

    async def refresh_token(self) -> SessionInfo:
        return await self._auth.refresh_token()

    async def get_session_status(self) -> SessionStatus:
        return await self._auth.get_session_status()

    def set_access_token(self, token: str) -> None:
        """Update the live SmartConnect client's token in place — lets a
        long-running process pick up a refreshed token without restarting."""
        self._auth.set_access_token(token)

    # ── Instrument resolution ─────────────────────────────────────────────────

    async def load_instrument_tokens(self, exchange: str = "NSE") -> int:
        """Bulk-load {tradingsymbol: (symboltoken, exch_seg)} for one exchange
        from Angel One's public instrument master dump, so every other call can
        resolve a symboltoken without an extra network round-trip.

        Idempotent per exchange: a non-empty cache for that exchange is left
        alone. Returns the number of tokens cached for that exchange. Never
        raises — a failed download degrades to an empty cache and callers that
        need a token will raise their own clear KeyError instead (see
        get_ltp/get_ohlcv), matching KiteAdapter's fail-soft-then-fail-loud
        pattern.
        """
        if self._token_cache.get(exchange):
            return len(self._token_cache[exchange])
        async with self._token_cache_lock:
            if self._token_cache.get(exchange):
                return len(self._token_cache[exchange])
            try:
                async with aiohttp.ClientSession() as http:
                    async with asyncio.timeout(60.0):
                        async with http.get(_INSTRUMENT_MASTER_URL) as resp:
                            resp.raise_for_status()
                            dump: list[dict[str, Any]] = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                _log.warning("angel_one_instrument_master_load_failed err=%s", exc)
                return 0
            rows = [r for r in dump if str(r.get("exch_seg")) == exchange]
            cache: dict[str, tuple[str, str]] = {}
            for row in rows:
                sym = row.get("symbol")
                token = row.get("token")
                if sym and token:
                    cache[str(sym)] = (str(token), exchange)
            if exchange == "NSE":
                # The codebase passes bare equity names ("RELIANCE") but the
                # scrip master lists them as "RELIANCE-EQ". Alias series EQ
                # only, after all raw keys.
                equity_alias_keys: set[str] = set()
                for row in rows:
                    sym = row.get("symbol")
                    token = row.get("token")
                    name = row.get("name")
                    if sym and token and name and str(sym).endswith("-EQ"):
                        if str(name) not in cache:
                            equity_alias_keys.add(str(name))
                        cache.setdefault(str(name), (str(token), exchange))
                # Index spots: the codebase passes Kite-style names ("NIFTY 50",
                # "NIFTY BANK", "INDIA VIX", bare "NIFTY"). Angel's docs use the
                # AMXIDX rows (99926000 ...) for candles/quotes, not the
                # token-26000 "NIFTY" row, so these aliases override raw keys
                # (findings B5, C-18) -- but never an equity's bare alias.
                skipped: list[str] = []
                for row in rows:
                    if str(row.get("instrumenttype")) != "AMXIDX":
                        continue
                    sym = row.get("symbol")
                    token = row.get("token")
                    name = row.get("name")
                    if not (sym and token):
                        continue
                    entry = (str(token), exchange)
                    keys = [str(sym), str(sym).upper()]
                    if name and str(name) in _INDEX_BARE_NAMES:
                        keys.append(str(name))
                    for key in keys:
                        if key in equity_alias_keys:
                            skipped.append(key)
                            continue
                        cache[key] = entry
                if skipped:
                    _log.warning(
                        "angel_one_index_alias_skipped keys=%s reason=would_replace_equity",
                        sorted(set(skipped)),
                    )
            if cache:
                self._token_cache[exchange] = cache
            return len(cache)

    async def _resolve_token(self, symbol: str, exchange: str = "NSE") -> tuple[str, str]:
        """Resolve a bare or exchange-prefixed symbol to (symboltoken, exchange).

        An ``EXCH:`` prefix (e.g. ``NFO:NIFTY27OCT26FUT``) selects that
        exchange; otherwise ``exchange`` is used. Lookup is exact first, then
        upper-cased (so "nifty 50" finds the "NIFTY 50" index alias).
        """
        prefix, sep, bare = symbol.rpartition(":")
        if sep and prefix:
            exchange = prefix.upper()
        if exchange not in self._token_cache:
            await self.load_instrument_tokens(exchange)
        cache = self._token_cache.get(exchange, {})
        hit = cache.get(bare) or cache.get(bare.upper())
        if hit is None:
            raise KeyError(f"no Angel One symboltoken found for {symbol!r} on {exchange}")
        return hit

    # ── Market Data ────────────────────────────────────────────────────────────

    async def subscribe_ticks(self, tokens: list[str]) -> None:
        self._subscribed_tokens.update(tokens)

    async def unsubscribe_ticks(self, tokens: list[str]) -> None:
        self._subscribed_tokens.difference_update(tokens)

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """Fetch LTPs via batched getMarketData("LTP", {exchange: [tokens]}) —
        up to 50 tokens per exchange per request (findings B1, C-20) — rather
        than one ltpData call per symbol. Rate-limited on the "quote" bucket
        at 1 req/s (the docs text says 1/s, their table 10/s; use the safe one).

        Result is keyed by the exact requested strings (incl. any ``NSE:``
        prefix). A symbol that cannot be resolved raises KeyError (before any
        request is sent); a symbol the broker returns no price for (failed
        batch, or listed as unfetched) is simply absent.
        """
        resolved = {sym: await self._resolve_token(sym) for sym in symbols}
        tokens_by_exchange: dict[str, list[str]] = {}
        for token, exch in resolved.values():
            bucket = tokens_by_exchange.setdefault(exch, [])
            if token not in bucket:
                bucket.append(token)

        prices: dict[tuple[str, str], float] = {}
        for exch, tokens in tokens_by_exchange.items():
            for start in range(0, len(tokens), _MARKET_DATA_MAX_TOKENS):
                chunk = tokens[start : start + _MARKET_DATA_MAX_TOKENS]
                await self._limiter.acquire("quote")
                response: dict[str, Any] = await asyncio.wait_for(
                    asyncio.to_thread(self._sc.getMarketData, "LTP", {exch: chunk}), timeout=10.0
                )
                if not response.get("status"):
                    _log.warning(
                        "angel_one_ltp_batch_failed exchange=%s n=%d msg=%s",
                        exch,
                        len(chunk),
                        response.get("message"),
                    )
                    continue
                for row in (response.get("data") or {}).get("fetched") or []:
                    prices[(exch, str(row.get("symbolToken")))] = float(row.get("ltp") or 0.0)

        return {
            sym: prices[(exch, token)]
            for sym, (token, exch) in resolved.items()
            if (exch, token) in prices
        }

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        """Fetch OHLCV history. Returns DataFrame with columns: date,open,high,low,close,volume.

        `interval` is the Kite Connect vocabulary this codebase's callers pass
        regardless of active broker (e.g. "5minute", "day") — translated to
        SmartAPI's ONE_MINUTE/.../ONE_DAY constants via _INTERVAL_TO_ANGEL.

        SmartAPI caps one getCandleData request at a per-interval number of
        days (_MAX_DAYS_PER_REQUEST), so [now-days, now] is split into
        contiguous windows oldest→newest, each paced on the "historical"
        bucket. An empty window is skipped; a failing window raises (never a
        silent partial history). Rows are merged, sorted ascending and
        de-duplicated on timestamp (adjacent windows share their boundary);
        ``date`` is a tz-aware Asia/Kolkata datetime (rows arrive as
        "2023-09-06T11:15:00+05:30").
        """
        angel_interval = _INTERVAL_TO_ANGEL.get(interval)
        if angel_interval is None:
            raise ValueError(
                f"unrecognized interval {interval!r} — expected one of "
                f"{sorted(_INTERVAL_TO_ANGEL)}"
            )
        token, exch = await self._resolve_token(symbol)

        to_dt = datetime.now(_IST)
        from_dt = to_dt - timedelta(days=days)
        # One day under the vendor max: whether its day-count is inclusive is
        # unverified, and a loud rejection on every multi-window fetch is worse
        # than one extra request.
        step = timedelta(days=_MAX_DAYS_PER_REQUEST[angel_interval] - 1)

        rows: list[list[Any]] = []
        win_start = from_dt
        while True:
            win_end = min(win_start + step, to_dt)
            rows.extend(
                await self._candle_window(symbol, exch, token, angel_interval, win_start, win_end)
            )
            if win_end >= to_dt:
                break
            win_start = win_end

        if not rows:
            # tz-aware date dtype even when empty so callers using .dt never fail
            return pd.DataFrame(
                {
                    c: pd.Series(dtype=_DATE_DTYPE if c == "date" else "float64")
                    for c in _OHLCV_COLUMNS
                }
            )
        # Response rows are arrays: [timestamp, open, high, low, close, volume]
        # (verified 2026-09-22 against docs/Historical's published example).
        df = pd.DataFrame(
            [
                {
                    "date": r[0],
                    "open": r[1],
                    "high": r[2],
                    "low": r[3],
                    "close": r[4],
                    "volume": r[5],
                }
                for r in rows
            ]
        )
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(_IST).astype(_DATE_DTYPE)
        return (
            df.drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )

    async def _candle_window(
        self,
        symbol: str,
        exch: str,
        token: str,
        angel_interval: str,
        win_start: datetime,
        win_end: datetime,
    ) -> list[list[Any]]:
        """One getCandleData request for [win_start, win_end]; raises on failure."""
        params = {
            "exchange": exch,
            "symboltoken": token,
            "interval": angel_interval,
            "fromdate": win_start.strftime("%Y-%m-%d %H:%M"),
            "todate": win_end.strftime("%Y-%m-%d %H:%M"),
        }
        _t_req = time.monotonic()
        await self._limiter.acquire("historical")
        try:
            response: dict[str, Any] = await asyncio.wait_for(
                asyncio.to_thread(self._sc.getCandleData, params), timeout=10.0
            )
        except TimeoutError as exc:
            # Same rationale as kite/adapter.py's get_ohlcv timeout guard: a
            # hung call here must not block the caller forever.
            raise TimeoutError(f"getCandleData timed out for {symbol!r} after 10s") from exc
        _t_done = time.monotonic()
        if _t_done - _t_req > 5.0 and self._slow_call_logs < 10:
            self._slow_call_logs += 1
            _log.warning("angel_one_candle_slow sym=%s total=%.1fs", symbol, _t_done - _t_req)

        if not response.get("status"):
            raise ValueError(f"getCandleData failed for {symbol!r}: {response.get('message')}")
        data: list[list[Any]] = response.get("data") or []
        return data

    async def get_depth(self, symbol: str) -> MarketDepth:
        token, exch = await self._resolve_token(symbol)
        await self._limiter.acquire("quote")
        response: dict[str, Any] = await asyncio.wait_for(
            asyncio.to_thread(self._sc.getMarketData, "FULL", {exch: [token]}), timeout=10.0
        )
        fetched: list[dict[str, Any]] = (response.get("data") or {}).get("fetched") or []
        if not fetched:
            raise ValueError(f"no market data returned by Angel One for {symbol!r}")
        return normalizer.normalize_depth(symbol, fetched[0])

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        await self.load_instrument_tokens(exchange)
        async with aiohttp.ClientSession() as http:
            async with asyncio.timeout(60.0):
                async with http.get(_INSTRUMENT_MASTER_URL) as resp:
                    resp.raise_for_status()
                    dump: list[dict[str, Any]] = await resp.json(content_type=None)
        instruments = [_to_instrument(r) for r in dump if str(r.get("exch_seg")) == exchange]
        if exchange != "NSE":
            return instruments
        # Series-EQ rows are exposed by bare name so downstream equity-universe
        # filters (which compare bare symbols) match.
        return [
            replace(inst, symbol=inst.name) if inst.symbol.endswith("-EQ") and inst.name else inst
            for inst in instruments
        ]

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        if not symbols:
            return {}
        tokens_by_symbol: dict[str, tuple[str, str]] = {}
        for sym in symbols:
            tokens_by_symbol[sym] = await self._resolve_token(sym, exchange="NFO")

        by_exchange: dict[str, list[str]] = {}
        for _sym, (token, exch) in tokens_by_symbol.items():
            by_exchange.setdefault(exch, []).append(token)

        await self._limiter.acquire("quote")
        response: dict[str, Any] = await asyncio.wait_for(
            asyncio.to_thread(self._sc.getMarketData, "FULL", by_exchange), timeout=10.0
        )
        fetched: list[dict[str, Any]] = (response.get("data") or {}).get("fetched") or []
        by_token = {str(row.get("symbolToken")): row for row in fetched}

        result: dict[str, OptionQuote] = {}
        for sym, (token, _exch) in tokens_by_symbol.items():
            row = by_token.get(token)
            if row is not None:
                result[sym] = normalizer.normalize_option_quote(sym, row)
        return result

    # ── Orders ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _current_variety(strict: bool = True) -> str:
        """NORMAL during the live session, AMO in the after-market window, per
        config/market_hours.json (src/market/sessions.py — same single source
        of truth Kite's adapter uses). See kite/adapter.py's identical helper
        for the no-order-gap rationale."""
        now = datetime.now(_IST)
        variety = sessions.order_variety_at(now)
        if variety == sessions.REGULAR:
            return "NORMAL"
        if variety == sessions.AMO:
            return "AMO"
        if strict:
            raise OrderWindowClosed(
                f"no order window open at {now:%H:%M} IST (see config/market_hours.json)"
            )
        return "NORMAL" if now.time() >= sessions.regular_close() else "AMO"

    async def place_order(self, order: OrderRequest) -> str:
        token, exch = await self._resolve_token(order.symbol, order.exchange)
        angel_type, variety_override = _ORDER_TYPE_MAP.get(
            order.order_type.upper(), (order.order_type, None)
        )
        variety = variety_override or self._current_variety()
        angel_product = _PRODUCT_TO_ANGEL.get(order.product, order.product)

        params: dict[str, Any] = {
            "variety": variety,
            "tradingsymbol": order.symbol,
            "symboltoken": token,
            "transactiontype": order.direction,
            "exchange": exch,
            "ordertype": angel_type,
            "producttype": angel_product,
            "duration": order.validity,
            "quantity": str(order.quantity),
            "price": str(_round_to_tick(order.price)) if order.price is not None else "0",
        }
        if order.trigger_price is not None:
            params["triggerprice"] = str(_round_to_tick(order.trigger_price))
        if order.tag:
            params["ordertag"] = order.tag[:20]

        _log.debug(
            "angel_place_order_received symbol=%s exchange=%s direction=%s qty=%s "
            "product=%s(%s) order_type=%s(%s) price=%s variety=%s",
            order.symbol,
            exch,
            order.direction,
            order.quantity,
            order.product,
            angel_product,
            order.order_type,
            angel_type,
            order.price,
            variety,
        )
        await self._limiter.acquire("order")
        order_id = await asyncio.to_thread(self._sc.placeOrder, params)
        if not order_id:
            raise RuntimeError(f"Angel One placeOrder returned no order id for {order.symbol!r}")
        _log.debug("angel_place_order_found symbol=%s broker_order_id=%s", order.symbol, order_id)
        return str(order_id)

    async def modify_order(self, order_id: str, params: dict[str, Any]) -> str:
        await self._limiter.acquire("order")
        _log.debug("angel_modify_order_received order_id=%s params=%s", order_id, params)
        angel_params = {"variety": self._current_variety(strict=False), "orderid": order_id}
        angel_params.update(params)
        result: dict[str, Any] = await asyncio.to_thread(self._sc.modifyOrder, angel_params)
        new_id = str(result.get("data", {}).get("orderid", order_id))
        _log.debug("angel_modify_order_found order_id=%s result_order_id=%s", order_id, new_id)
        return new_id

    async def cancel_order(self, order_id: str) -> bool:
        await self._limiter.acquire("order")
        _log.debug("angel_cancel_order_received order_id=%s", order_id)
        await asyncio.to_thread(self._sc.cancelOrder, order_id, self._current_variety(strict=False))
        _log.debug("angel_cancel_order_found order_id=%s action=cancelled", order_id)
        return True

    async def get_order_status(self, order_id: str) -> Order:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.orderBook)
        for raw in response.get("data") or []:
            if str(raw.get("orderid")) == order_id:
                normalized = normalizer.normalize_order(raw)
                _log.debug(
                    "angel_get_order_status_found order_id=%s status=%s",
                    order_id,
                    normalized.status,
                )
                return normalized
        _log.debug("angel_get_order_status_found order_id=%s status=NOT_FOUND", order_id)
        raise KeyError(f"Order {order_id!r} not found in today's order book")

    async def get_all_orders(self) -> list[Order]:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.orderBook)
        return [normalizer.normalize_order(r) for r in response.get("data") or []]

    async def get_trades(self, order_id: str) -> list[Fill]:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.tradeBook)
        return [
            Fill(
                order_id=str(t["orderid"]),
                trade_id=str(t.get("fillid", "")),
                quantity=int(t.get("fillsize") or 0),
                price=float(t.get("fillprice") or 0.0),
                exchange_time=normalizer._parse_ts(t.get("filltime")),  # noqa: SLF001
            )
            for t in response.get("data") or []
            if str(t.get("orderid")) == order_id
        ]

    # ── Positions & Portfolio ──────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """SmartAPI's getPosition() rows have no live ltp/pnl field (unlike
        Kite) — see normalizer.normalize_position's docstring. Enriches each
        row with a real get_ltp() call before normalizing."""
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.position)
        raw_positions: list[dict[str, Any]] = response.get("data") or []
        if not raw_positions:
            return []

        symbols = [str(r["tradingsymbol"]) for r in raw_positions]
        ltp_map = await self.get_ltp(symbols)
        return [
            normalizer.normalize_position(r, ltp=ltp_map.get(str(r["tradingsymbol"]), 0.0))
            for r in raw_positions
        ]

    async def get_holdings(self) -> list[Holding]:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.allholding)
        raw_holdings: list[dict[str, Any]] = (response.get("data") or {}).get("holdings") or []
        return [
            Holding(
                symbol=str(h["tradingsymbol"]),
                exchange=str(h.get("exchange", "NSE")),
                quantity=int(h.get("quantity") or 0),
                average_price=float(h.get("averageprice") or 0.0),
                last_price=float(h.get("ltp") or 0.0),
                pnl=float(h.get("profitandloss") or 0.0),
                t1_quantity=int(h.get("t1quantity") or 0),
            )
            for h in raw_holdings
        ]

    async def get_margins(self) -> Margin:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(self._sc.rmsLimit)
        return _margin_from_angel(response.get("data") or {})

    async def get_order_margin(self, orders: list[OrderRequest]) -> OrderMarginResult:
        """Real pre-trade basket margin via ``POST margin/v1/batch`` (SDK
        ``getMarginApi``, route ``api.margin.api``; findings B2, C-21).

        The API takes up to 50 positions and returns ONE basket figure
        (``totalMarginRequired``, hedge benefit already netted), so ``per_leg``
        is empty and ``raw`` is the response ``data`` dict. More than 50 legs
        raise ValueError (never truncated); a refused call or a response
        without ``totalMarginRequired`` raises AngelOneMarginError (a missing
        total must never read as zero margin).

        GATED: raises NotImplementedError unless capability_matrix.json's
        angel_one.margin_api_verified is true (default false), so the router
        falls through to the next provider.

        UNVERIFIED (Phase 0 findings, Appendix item 6) — confirm with a paper
        basket against a known margin before gating entries on this:
        * ``qty``: whether the API wants units or lots is not documented (its
          sample uses 50 on NFO tokens). This passes ``OrderRequest.quantity``
          in UNITS unchanged, because the system sizes in units.
        * ``productType``: only "INTRADAY" is shown in the docs. The canonical
          product is passed through ``_PRODUCT_TO_ANGEL`` ("MARGIN" for NFO
          carry-forward entries may need "CARRYFORWARD" instead).
        * ``orderType``: only MARKET and LIMIT are sent; SL/SL-M raise
          ValueError rather than guess an enum. ``price`` is 0 for MARKET (and
          for a LIMIT with no price; whether 0 then means LTP is unverified).
        """
        if not _CAPS.get("margin_api_verified", False):
            # Checked before any validation or request. NotImplementedError is a
            # capability gap the market-data router skips without cooling the
            # provider; a wrong figure (or errors from a mis-mapped enum) would not be.
            raise NotImplementedError(
                "Angel One margin API is implemented but UNVERIFIED (qty units/lots, "
                "productType enum, price-0 semantics); set capability "
                "margin_api_verified=true after a paper-basket check"
            )
        if not orders:
            raise ValueError("get_order_margin: no orders to price")
        if len(orders) > _MARGIN_MAX_POSITIONS:
            raise ValueError(
                f"Angel One margin API takes at most {_MARGIN_MAX_POSITIONS} positions per "
                f"request, got {len(orders)}"
            )

        positions: list[dict[str, Any]] = []
        for o in orders:
            order_type = o.order_type.upper()
            if order_type not in ("MARKET", "LIMIT"):
                raise ValueError(
                    f"Angel One margin API orderType not verified for {o.order_type!r} "
                    "(only MARKET and LIMIT are supported)"
                )
            token, exch = await self._resolve_token(o.symbol, o.exchange)
            positions.append(
                {
                    "exchange": exch,
                    "qty": o.quantity,
                    "price": o.price if order_type == "LIMIT" and o.price else 0,
                    "productType": _PRODUCT_TO_ANGEL.get(o.product, o.product),
                    "token": token,
                    "tradeType": o.direction,
                    "orderType": order_type,
                }
            )

        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.wait_for(
            asyncio.to_thread(self._sc.getMarginApi, {"positions": positions}), timeout=10.0
        )
        errorcode = str(response.get("errorcode") or "")
        if not response.get("status") or errorcode:
            raise AngelOneMarginError(
                f"Angel One margin API refused the basket: errorcode={errorcode!r} "
                f"message={response.get('message')!r}"
            )
        data: dict[str, Any] = response.get("data") or {}
        if data.get("totalMarginRequired") is None:
            raise AngelOneMarginError(
                f"Angel One margin API response has no totalMarginRequired: {response!r}"
            )
        return OrderMarginResult(total_margin=float(data["totalMarginRequired"]), raw=data)

    # ── GTT Orders (single-trigger only — see place_gtt) ─────────────────────

    async def place_gtt(self, gtt: GTTRequest) -> str:
        """Angel One's GTT ("Rule") API supports exactly one trigger + one
        price per rule (verified 2026-09-22 against docs/Gtt's createRule
        schema: price + triggerprice, no separate target fields) — unlike
        Kite's GTT, which places a two-leg SL+target OCO in a single call.
        A TWO_LEG request has no broker-side equivalent here; refusing it
        loudly (rather than silently dropping the target leg) matches this
        codebase's fail-safe convention (see kite/adapter.py's identical
        refusal for non-LIMIT GTT legs). Callers needing SL+target protection
        on Angel One must use the manual order-pair fallback path
        (capability_matrix.json's angel_one.gtt_fallback), not GTT.
        """
        if gtt.gtt_type != "SINGLE_LEG":
            raise ValueError(
                f"Angel One GTT rules support a single trigger only, got "
                f"gtt_type={gtt.gtt_type!r} — see capability_matrix.json "
                "angel_one.gtt_fallback for the SL+target alternative"
            )
        token, exch = await self._resolve_token(gtt.symbol)
        angel_product = _PRODUCT_TO_ANGEL.get(gtt.product, gtt.product)
        sl_trigger = _round_to_tick(gtt.sl_trigger)
        sl_price = _round_to_tick(gtt.sl_price if gtt.sl_price is not None else gtt.sl_trigger)

        await self._limiter.acquire("order")
        response: int = await asyncio.to_thread(
            self._sc.gttCreateRule,
            {
                "tradingsymbol": gtt.symbol,
                "symboltoken": token,
                "exchange": exch,
                "producttype": angel_product,
                "transactiontype": "SELL",
                "price": sl_price,
                "qty": gtt.quantity,
                "triggerprice": sl_trigger,
                "disclosedqty": "0",
            },
        )
        return str(response)

    async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str:
        """Recognized params: ``sl_trigger`` and ``quantity`` — mirrors
        KiteAdapter.modify_gtt's contract. Re-fetches the current rule first:
        SmartAPI's modifyRule example only shows id/symboltoken/exchange/
        price/qty/triggerprice/disclosedqty, but AB9001 "Invalid Parameters"
        suggests the safer path (proven necessary for Kite's equivalent call
        on 2026-08-28, see kite/adapter.py) is to resend the full known spec
        rather than a partial diff."""
        await self._limiter.acquire("general")
        current: dict[str, Any] = await asyncio.to_thread(self._sc.gttDetails, int(gtt_id))
        rule: dict[str, Any] = current.get("data") or {}
        if not rule:
            raise KeyError(f"GTT {gtt_id!r} not found at broker")

        price = _round_to_tick(float(rule.get("price") or 0.0))
        trigger = _round_to_tick(float(rule.get("triggerprice") or 0.0))
        qty = int(float(rule.get("qty") or 0))

        if params.get("sl_trigger") is not None:
            trigger = _round_to_tick(float(params["sl_trigger"]))
            price = trigger  # single-leg rule: limit price tracks its own trigger
        if params.get("quantity") is not None:
            qty = int(params["quantity"])

        await self._limiter.acquire("order")
        response: int = await asyncio.to_thread(
            self._sc.gttModifyRule,
            {
                "id": int(gtt_id),
                "symboltoken": rule.get("symboltoken"),
                "exchange": rule.get("exchange"),
                "price": price,
                "qty": qty,
                "triggerprice": trigger,
                "disclosedqty": rule.get("disclosedqty", "0"),
            },
        )
        return str(response)

    async def cancel_gtt(self, gtt_id: str) -> bool:
        await self._limiter.acquire("general")
        current: dict[str, Any] = await asyncio.to_thread(self._sc.gttDetails, int(gtt_id))
        rule: dict[str, Any] = current.get("data") or {}

        await self._limiter.acquire("order")
        await asyncio.to_thread(
            self._sc.gttCancelRule,
            {
                "id": int(gtt_id),
                "symboltoken": rule.get("symboltoken"),
                "exchange": rule.get("exchange", "NSE"),
            },
        )
        return True

    async def get_all_gtts(self) -> list[GTTOrder]:
        await self._limiter.acquire("general")
        response: dict[str, Any] = await asyncio.to_thread(
            self._sc.gttLists, ["NEW", "CANCELLED", "TRIGGERED", "EXPIRED"], 1, 100
        )
        rows: list[dict[str, Any]] = response.get("data") or []
        result: list[GTTOrder] = []
        for r in rows:
            status = _GTT_STATUS_MAP.get(str(r.get("status", "NEW")).upper(), "ACTIVE")
            result.append(
                GTTOrder(
                    gtt_id=str(r.get("id", "")),
                    symbol=str(r.get("tradingsymbol", "")),
                    status=status,
                    gtt_type="SINGLE_LEG",
                    sl_trigger=float(r.get("triggerprice") or 0.0),
                    target_trigger=None,
                    raw=r,
                )
            )
        return result

    # ── Capability flags ───────────────────────────────────────────────────────

    def supports_gtt(self) -> bool:
        return bool(_CAPS.get("gtt_native", False))

    def supports_bracket_order(self) -> bool:
        return bool(_CAPS.get("bracket_order", False))

    def supports_cover_order(self) -> bool:
        return bool(_CAPS.get("cover_order", False))

    # ── Normalization ──────────────────────────────────────────────────────────

    def normalize_tick(self, raw: dict[str, Any]) -> Tick:
        return normalizer.normalize_tick(raw)

    def normalize_order(self, raw: dict[str, Any]) -> Order:
        return normalizer.normalize_order(raw)

    def normalize_position(self, raw: dict[str, Any]) -> Position:
        return normalizer.normalize_position(raw)

    def normalize_error(self, raw: dict[str, Any]) -> BrokerError:
        return normalizer.normalize_error(raw)
