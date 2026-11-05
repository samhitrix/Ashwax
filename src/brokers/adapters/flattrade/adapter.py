"""
Flattrade (Pi API v2, Noren OMS) broker adapter.

Works: session methods (delegated to FlattradeAuth), capability flags
(capability_matrix.json), and market data (F2): instruments and symbol mapping
from the public scrip master, LTP / depth / option quotes via `GetQuotes`, and
candles via `TPSeries` (intraday) / `EODChartData` (daily). Orders, margin,
positions, holdings, funds and the order-side normalizers (F3) are below.

Orders (findings A1, A3, A7, C-4, C-6): only LMT / SL-LMT are sent. MARKET becomes a
marketable LMT off LTP and SL-M an SL-LMT beyond the trigger, both by the protection
band `flattrade.market_protection_band_pct` in config/execution.json (a risk
parameter; see orders.py). Before the first order of a session the account's
enabled price types / products / exchanges are read from `UserDetails` and an order
the account cannot send raises FlattradeError. An ambiguous transport failure
(request may have reached Flattrade) on PlaceOrder / ModifyOrder / CancelOrder is
never retried blindly: the order book / order history is read to learn the truth.
GTT / OCO stay NotImplementedError (findings A4, UNVERIFIED).

Throughput: `GetQuotes` takes one scrip per call and the non-order bucket is
40/s and 200/min (findings A1, C-7), so N symbols cost N calls — a 100-leg option
chain takes about 30 s. Flattrade option chains are slow until the websocket
feed (findings A6) is wired.

Intraday bar labels: whether a TPSeries timestamp marks the bar's START (Kite's
convention, which the orchestrator's `_refresh_one` relies on: it keeps a bar only
when ts + bar_min <= now) or its END is UNVERIFIED (BAR_LABEL_CONVENTION). Bars
are passed through unchanged; a WARNING fires once per process when stamps are
not aligned to the session grid. Flattrade must NOT be the primary `ohlcv`
provider until `python scripts/compare_provider_data.py --a kite --b flattrade
--interval 5minute` confirms the convention. Daily bars are stamped 00:00 IST.

LIVE ORDERS ARE OFF: place_order / modify_order / cancel_order raise
FlattradeLiveOrdersDisabled before anything is sent until capability_matrix.json
flattrade.live_orders_verified is flipped to true after a 1-lot live smoke test
(LIMIT, SL-LMT, MARKET conversion, modify, cancel, SingleOrdHist read) and the
exchange-approval question (Flattrade terms 2(e)) is settled. Reasons: every field
is from the findings, not from a live call; MARKET is a marketable LIMIT that can
come back unfilled while some callers treat MARKET as filled; and the SL-M -> SL-LMT
conversion has NO gap watchdog (a gap through the band leaves the stop resting
unfilled; nothing re-prices it - NOT BUILT).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.brokers.adapters.flattrade import normalizer, orders
from src.brokers.adapters.flattrade.auth import FlattradeAuth
from src.brokers.adapters.flattrade.client import (
    FlattradeError,
    FlattradeSessionExpired,
    FlattradeTransportError,
)
from src.brokers.adapters.flattrade.symbols import FlattradeSymbols, Resolved
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
from src.market import sessions

if TYPE_CHECKING:
    import pandas as pd

    from src.brokers.adapters.flattrade.client import FlattradeClient

_log = logging.getLogger(__name__)  # nests under "src.brokers" -> brokers.log
_IST = ZoneInfo("Asia/Kolkata")

# Kite-vocabulary interval (what this codebase's callers pass) -> TPSeries `intrv`
# minutes (findings A3: 1/3/5/10/15/30/60/120). "day" goes to EODChartData.
_TPSERIES_INTERVALS: dict[str, str] = {
    "minute": "1",
    "3minute": "3",
    "5minute": "5",
    "10minute": "10",
    "15minute": "15",
    "30minute": "30",
    "60minute": "60",
    "120minute": "120",
}
_DAILY_INTERVAL = "day"
# TPSeries max span per request is UNVERIFIED (findings A3, C-8): chunk defensively.
_TPSERIES_MAX_WINDOW = timedelta(days=30)
# In-flight GetQuotes calls; the limiter's general bucket does the real pacing.
_DEFAULT_QUOTE_CONCURRENCY = 8
# Above this many legs in one get_option_quotes call, warn (once) that it is slow.
_SLOW_CHAIN_LEGS = 40

# TPSeries bar label (start vs end of bar) is not documented; see module docstring.
BAR_LABEL_CONVENTION = "unverified"
# NSE session open: intraday bars (Kite) sit on a grid anchored here (09:15, 09:25 ...
# for 10-minute), not on clock multiples.
_SESSION_OPEN_MINUTE = 9 * 60 + 15
_warned_bar_alignment = False  # once per process
# A first daily bar later than from + this suggests a server-side span cap (the
# longest NSE closure is a few days).
_EOD_TRUNCATION_SLACK = timedelta(days=10)

with (Path(__file__).parent.parent.parent / "capability_matrix.json").open(encoding="utf-8") as _f:
    _CAPS: dict[str, Any] = json.load(_f).get("flattrade", {})


# An order found in the book after an ambiguous PlaceOrder must carry this attempt's
# unique remarks AND an entry time inside [attempt - skew, lookup + skew]; the skew
# tolerates host/OMS clock drift. A row whose entry time does not parse is not
# accepted (ordenttm's format is UNVERIFIED; the caller then reconciles manually).
_AMBIGUOUS_CLOCK_SKEW = timedelta(seconds=120)
# OrderBook reads after an ambiguous PlaceOrder. The failed request already waited
# up to the 15 s HTTP timeout, so an accepted order is normally listed by the first
# read; the extra reads cover the OMS listing it a moment late. 3 reads 1 s apart
# cost 3 general-bucket calls (40/s, 200/min) and ~2 s.
_AMBIGUOUS_POLLS = 3
_AMBIGUOUS_POLL_DELAY_S = 1.0
# remarks = <tag prefix>-<8 hex>: the hex is unique per attempt so the order-book
# lookup can never match an older order that shares a constant tag.
_REMARKS_UID_LEN = 8
_GTT_UNVERIFIED = (
    "Flattrade GTT/OCO is UNVERIFIED (findings A4: x/y leg mapping, trigger direction, "
    "NFO and product support, SL-LMT legs); supports_gtt() is False - use the manual "
    "SL/target order pair (capability gtt_fallback)"
)


class FlattradeLiveOrdersDisabled(FlattradeError, NotImplementedError):
    """Live Flattrade orders are switched off (capability live_orders_verified=false)."""


def _now() -> datetime:
    return datetime.now(_IST)


def _require_live_orders(action: str) -> None:
    """Refuse any order action until flattrade.live_orders_verified is true."""
    if not _CAPS.get("live_orders_verified", False):
        raise FlattradeLiveOrdersDisabled(
            f"Flattrade {action} refused: live orders are disabled until capability_matrix.json "
            "flattrade.live_orders_verified is set true after a 1-lot live smoke test and "
            "exchange approval (Flattrade terms 2(e)); nothing was sent"
        )


# SingleOrdHist `rpt` values that mark the order's creation (always its oldest row).
_CREATION_REPORTS = frozenset({"PENDINGNEW", "NEWACK", "NEW"})


def _rpt_key(value: Any) -> str:
    return "".join(str(value or "").split()).replace("_", "").upper()


def _is_amend_rejection(rpt: Any) -> bool:
    """A rejected modify / cancel report (e.g. "ModRejected"); the order lives on."""
    key = _rpt_key(rpt)
    return "REJ" in key and key not in ("REJECTED", "REJECT")


def _history_newest_first(rows: list[dict[str, Any]]) -> bool | None:
    """True/False when the SingleOrdHist array order can be detected, else None."""
    if len(rows) < 2:
        return False
    first_new = _rpt_key(rows[0].get("rpt")) in _CREATION_REPORTS
    last_new = _rpt_key(rows[-1].get("rpt")) in _CREATION_REPORTS
    if first_new != last_new:
        return last_new
    t_first = normalizer.parse_order_time(rows[0].get("norentm"))
    t_last = normalizer.parse_order_time(rows[-1].get("norentm"))
    if t_first is not None and t_last is not None and t_first != t_last:
        return t_first > t_last
    return None


def _unique_remarks(base: str) -> str:
    """`<base prefix>-<8 hex>` within REMARKS_MAX (or just the hex if base is empty)."""
    uid = uuid.uuid4().hex[:_REMARKS_UID_LEN]
    keep = orders.REMARKS_MAX - _REMARKS_UID_LEN - 1
    return f"{base[:keep]}-{uid}" if base else uid


def _order_variety(now: datetime) -> str:
    """REGULAR / AMO / CLOSED from config/market_hours.json (the Kite/Angel source)."""
    return sessions.order_variety_at(now)


def _epoch(dt: datetime) -> str:
    return str(int(dt.timestamp()))


_BOOK_ENDPOINTS = frozenset({"PositionBook", "OrderBook", "TradeBook", "Holdings"})


def _is_no_data(exc: FlattradeError, endpoint: str) -> bool:
    """True for the documented `"No Data : "` reply (findings A7). On the account BOOK
    endpoints an empty book also comes back as `Error Occurred : 5 "no data"` (seen live on
    PositionBook, 2026-10-17); on every other endpoint (candles) that text stays an error:
    fail loud."""
    if isinstance(exc, FlattradeSessionExpired | FlattradeTransportError):
        return False
    msg = str(exc)
    prefix = f"{endpoint}: "
    emsg = msg[len(prefix) :] if msg.startswith(prefix) else msg
    text = emsg.strip().lower()
    if endpoint in _BOOK_ENDPOINTS and text.startswith("error occurred : 5") and "no data" in text:
        return True
    return text.startswith("no data")


def _warn_if_misaligned(df: pd.DataFrame, interval_min: int, symbol: str) -> None:
    """WARN once per process if intraday stamps are off the session grid (second != 0,
    or minutes since 09:15 not a multiple of the interval). Behaviour is unchanged:
    this only surfaces a possible bar-label convention mismatch (UNVERIFIED)."""
    global _warned_bar_alignment
    if _warned_bar_alignment or df.empty or interval_min > 60:
        return
    dates = df["date"]
    minutes = dates.dt.hour * 60 + dates.dt.minute - _SESSION_OPEN_MINUTE
    bad = (dates.dt.second != 0) | (minutes % interval_min != 0)
    if bool(bad.any()):
        _warned_bar_alignment = True
        _log.warning(
            "flattrade_bar_stamps_misaligned symbol=%s interval_min=%d first_bad=%s "
            "bar_label_convention=%s - do not use flattrade as primary ohlcv until "
            "scripts/compare_provider_data.py --a kite --b flattrade --interval 5minute agrees",
            symbol,
            interval_min,
            dates[bad].iloc[0],
            BAR_LABEL_CONVENTION,
        )


class FlattradeAdapter(BrokerInterface):
    """Flattrade adapter; holds the day's session via FlattradeAuth."""

    def __init__(
        self,
        auth: FlattradeAuth,
        *,
        symbols: FlattradeSymbols | None = None,
        max_concurrent_quotes: int = _DEFAULT_QUOTE_CONCURRENCY,
        protection_band: float | None = None,
    ) -> None:
        """`protection_band` overrides config/execution.json
        `flattrade.market_protection_band_pct` (a fraction; invalid values raise)."""
        self._auth = auth
        self._band = Decimal(
            str(
                orders.validate_band(protection_band)
                if protection_band is not None
                else orders.load_protection_band()
            )
        )
        self._ticks: dict[tuple[str, str], Decimal] = {}
        self._user_details: dict[str, Any] | None = None
        self._user_details_lock = asyncio.Lock()
        self._placed_ids: set[str] = set()
        self._warned_holdings_qty = False
        self._symbols = symbols if symbols is not None else FlattradeSymbols()
        self._quote_slots = asyncio.Semaphore(max_concurrent_quotes)
        self._subscribed_tokens: set[str] = set()
        self._warned_ticks_not_wired = False
        self._warned_slow_chain = False
        self._warned_no_oi = False

    @classmethod
    def from_env(cls) -> FlattradeAdapter:
        """Construct from FLATTRADE_* environment variables."""
        return cls(FlattradeAuth.from_env())

    @property
    def _client(self) -> FlattradeClient:
        return self._auth.client

    # ── Authentication ─────────────────────────────────────────────────────────

    async def login(self) -> SessionInfo:
        return await self._auth.login()

    async def refresh_token(self) -> SessionInfo:
        return await self._auth.refresh_token()

    async def get_session_status(self) -> SessionStatus:
        return await self._auth.get_session_status()

    def set_access_token(self, token: str) -> None:
        """Pick up a freshly exchanged daily token without a restart (re-reads the
        account's order enablement before the next order)."""
        self._auth.set_access_token(token)
        self._user_details = None

    # ── Instruments (F2) ───────────────────────────────────────────────────────

    async def load_instrument_tokens(self, exchange: str = "NSE") -> int:
        """Load one exchange's scrip master into the symbol cache.

        Idempotent; never raises (a failed download logs and returns 0, and a later
        lookup raises KeyError) - the Angel One / Kite fail-soft-then-fail-loud pattern.
        """
        return await self._symbols.load(exchange)

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        """All instruments of `exchange` from the scrip master.

        NSE `-EQ` rows are exposed by their bare name (`RELIANCE`) so equity-universe
        filters match; everything else keeps its tradingsymbol. `tick_size` is the
        DEFAULT 0.05 placeholder (no tick column in the file; the real tick is
        `GetQuotes.ti`).

        Raises:
            NotImplementedError: exchange not supported here (router falls through).
            FlattradeError: the scrip master could not be loaded (loud, unlike load).
        """
        if not self._symbols.is_supported(exchange):
            raise NotImplementedError(f"Flattrade scrip master for {exchange!r} not supported")
        instruments = await self._symbols.instruments(exchange)
        if not instruments:
            raise FlattradeError(f"Flattrade scrip master for {exchange} unavailable")
        if exchange != "NSE":
            return instruments
        return [
            replace(i, symbol=i.symbol[: -len("-EQ")])
            if i.instrument_type == "EQ" and i.symbol.endswith("-EQ")
            else i
            for i in instruments
        ]

    # ── Market data (F2) ───────────────────────────────────────────────────────

    async def subscribe_ticks(
        self,
        tokens: list[str],
        on_tick: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Record tokens only. The websocket consumer
        (`wss://piconnect.flattrade.in/PiConnectWSAPI/`, findings A6) is NOT wired, so
        no tick is ever delivered. `on_tick` (the tick ingester's call form) is accepted
        so that caller does not crash, and a WARNING says ticks will not arrive."""
        self._subscribed_tokens.update(tokens)
        if on_tick is not None and not self._warned_ticks_not_wired:
            self._warned_ticks_not_wired = True
            _log.warning(
                "flattrade_ticks_not_wired n=%d - websocket feed not implemented; "
                "no ticks will be delivered",
                len(tokens),
            )

    async def unsubscribe_ticks(self, tokens: list[str]) -> None:
        self._subscribed_tokens.difference_update(tokens)

    async def _quote(self, resolved: Resolved) -> dict[str, Any]:
        """One GetQuotes call (general bucket) under the concurrency semaphore."""
        async with self._quote_slots:
            reply = await self._client.call(
                "GetQuotes", {"exch": resolved.exchange, "token": resolved.token}
            )
        if not isinstance(reply, dict):
            raise FlattradeError(f"GetQuotes: unexpected response type {type(reply).__name__}")
        tick = orders.parse_tick(reply.get("ti"))
        if tick is not None:
            self._ticks[(resolved.exchange, resolved.token)] = tick
        return reply

    async def _quotes(self, resolved: dict[str, Resolved]) -> dict[str, dict[str, Any]]:
        """GetQuotes for each distinct scrip, keyed back to the requested symbols.

        A broker `Not_Ok` for one scrip omits it (logged). Session expiry, transport
        failures and anything unexpected propagate (provider ill health -> failover).
        """
        unique = list({(r.exchange, r.token): r for r in resolved.values()}.values())
        results = await asyncio.gather(*(self._quote(r) for r in unique), return_exceptions=True)
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for r, res in zip(unique, results, strict=True):
            if isinstance(res, FlattradeError) and not isinstance(
                res, FlattradeSessionExpired | FlattradeTransportError
            ):
                _log.warning(
                    "flattrade_quote_failed exch=%s tsym=%s err=%s",
                    r.exchange,
                    r.tradingsymbol,
                    res,
                )
                continue
            if isinstance(res, BaseException):
                raise res
            by_key[(r.exchange, r.token)] = res
        return {
            sym: by_key[(r.exchange, r.token)]
            for sym, r in resolved.items()
            if (r.exchange, r.token) in by_key
        }

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        """LTP per requested symbol (keys exactly as passed, e.g. ``NSE:RELIANCE``).

        One `GetQuotes` per distinct scrip (no bulk quote exists, findings C-7).
        Same contract as Angel One: an unresolvable symbol raises KeyError before any
        request; a scrip the broker refuses is absent; a reply without `lp` is 0.0.
        """
        resolved = {sym: await self._symbols.resolve(sym) for sym in symbols}
        quotes = await self._quotes(resolved)
        return {sym: normalizer.to_float(q.get("lp")) for sym, q in quotes.items()}

    async def get_depth(self, symbol: str) -> MarketDepth:
        """Best-5 depth from `GetQuotes` (`bp1..5/bq1..5/sp1..5/sq1..5`, `lp`, `ltt`)."""
        quote = await self._quote(await self._symbols.resolve(symbol))
        return normalizer.normalize_depth(symbol, quote)

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        """OptionQuote per requested leg (bare symbols default to NFO).

        One `GetQuotes` per leg: ltp/bid/ask/volume, and `oi` when present. GetQuotes
        has no IV or greeks (OptionQuote carries none; callers derive IV from the
        premium). Slow: the general bucket is 200/min, so >40 legs logs a WARNING once.
        A leg the broker refuses is absent; an unresolvable leg raises KeyError.
        """
        if not symbols:
            return {}
        resolved = {s: await self._symbols.resolve(s, default_exchange="NFO") for s in symbols}
        n_calls = len({(r.exchange, r.token) for r in resolved.values()})
        if n_calls > _SLOW_CHAIN_LEGS and not self._warned_slow_chain:
            self._warned_slow_chain = True
            _log.warning(
                "flattrade_option_chain_slow legs=%d - one GetQuotes per leg at 200/min "
                "(~%.0f s); Flattrade chains stay slow until the websocket feed is wired",
                n_calls,
                n_calls * 60 / 200,
            )
        quotes = await self._quotes(resolved)
        no_oi = [sym for sym, q in quotes.items() if q.get("oi") in (None, "")]
        if no_oi and not self._warned_no_oi:
            self._warned_no_oi = True
            _log.warning(
                "flattrade_quote_without_oi n=%d first=%s - OI reported as 0 "
                "(GetQuotes oi for NFO is UNVERIFIED); OI liquidity gates will see 0",
                len(no_oi),
                no_oi[0],
            )
        return {sym: normalizer.normalize_option_quote(sym, q) for sym, q in quotes.items()}

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        """Candles for [now - days, now]: columns date, open, high, low, close, volume.

        Intraday Kite-vocabulary intervals go to `TPSeries` in <=30-day windows (max
        span UNVERIFIED); "day" goes to one `EODChartData` call (`sym` like
        "NSE:RELIANCE-EQ"). Times are epoch seconds. Output is ascending,
        de-duplicated, tz-aware IST. A `"No Data"` reply is an empty window; any other
        error raises (never a silent partial history).
        """
        if interval != _DAILY_INTERVAL and interval not in _TPSERIES_INTERVALS:
            raise ValueError(
                f"unrecognized interval {interval!r} - expected one of "
                f"{sorted([*_TPSERIES_INTERVALS, _DAILY_INTERVAL])}"
            )
        resolved = await self._symbols.resolve(symbol)
        to_dt = _now()
        from_dt = to_dt - timedelta(days=days)

        elements: list[Any] = []
        if interval == _DAILY_INTERVAL:
            payload = {
                "sym": f"{resolved.exchange}:{resolved.tradingsymbol}",
                "from": _epoch(from_dt),
                "to": _epoch(to_dt),
            }
            elements.extend(await self._candles("EODChartData", payload))
        else:
            win_start = from_dt
            while True:
                win_end = min(win_start + _TPSERIES_MAX_WINDOW, to_dt)
                payload = {
                    "exch": resolved.exchange,
                    "token": resolved.token,
                    "st": _epoch(win_start),
                    "et": _epoch(win_end),
                    "intrv": _TPSERIES_INTERVALS[interval],
                }
                elements.extend(await self._candles("TPSeries", payload))
                if win_end >= to_dt:
                    break
                win_start = win_end
        daily = interval == _DAILY_INTERVAL
        df = normalizer.candles_to_frame(elements, daily=daily)
        if daily:
            if not df.empty and df["date"].iloc[0] > from_dt + _EOD_TRUNCATION_SLACK:
                _log.warning(
                    "flattrade_eod_possibly_truncated symbol=%s requested_from=%s first=%s - "
                    "EODChartData max span is UNVERIFIED (or the scrip listed later)",
                    symbol,
                    from_dt.date(),
                    df["date"].iloc[0].date(),
                )
        else:
            _warn_if_misaligned(df, int(_TPSERIES_INTERVALS[interval]), symbol)
        return df

    async def _candles(self, endpoint: str, payload: dict[str, str]) -> list[Any]:
        """One candle request; `"No Data"` -> []; a non-array reply raises."""
        try:
            reply = await self._client.call(endpoint, payload)
        except FlattradeError as exc:
            if _is_no_data(exc, endpoint):
                return []
            raise
        if not isinstance(reply, list):
            # list endpoints answer an object only on error (findings A3, C-10)
            raise FlattradeError(f"{endpoint}: expected an array, got {type(reply).__name__}")
        return reply

    # ── Orders (F3) ────────────────────────────────────────────────────────────

    async def _tick(self, resolved: Resolved) -> Decimal:
        """Tick size from the quote's `ti` (cached per scrip; one GetQuotes when
        unknown), else the 0.05 default."""
        key = (resolved.exchange, resolved.token)
        if key not in self._ticks:
            await self._quote(resolved)
        return self._ticks.get(key, orders.DEFAULT_TICK)

    async def _ensure_enabled(self, prctyp: str, prd: str, exch: str) -> None:
        """UserDetails once per session (cached only on success), then check the order."""
        async with self._user_details_lock:
            if self._user_details is None:
                reply = await self._client.call("UserDetails", {})
                if not isinstance(reply, dict):
                    raise FlattradeError("UserDetails: unexpected response shape")
                self._user_details = reply
            details = self._user_details
        orders.check_enabled(details, prctyp, prd, exch)

    @staticmethod
    def _validate(order: OrderRequest) -> tuple[str, str, str, str, str]:
        """Static checks before any request -> (order_type, prctyp, side, prd, exch)."""
        otype = order.order_type.upper()
        prctyp = orders.ORDER_TYPE_TO_PRCTYP.get(otype)
        side = orders.SIDE_TO_FT.get(order.direction.upper())
        prd = orders.PRODUCT_TO_FT.get(order.product.upper())
        if prctyp is None:
            raise ValueError(f"Flattrade order_type {order.order_type!r} not supported")
        if side is None:
            raise ValueError(f"Flattrade direction {order.direction!r} not supported")
        if prd is None:
            raise ValueError(f"Flattrade product {order.product!r} not supported")
        if order.validity.upper() not in orders.VALIDITIES:
            raise ValueError(f"Flattrade validity {order.validity!r} not supported")
        if not isinstance(order.quantity, int) or order.quantity <= 0:
            raise ValueError(f"Flattrade quantity must be a positive int, got {order.quantity!r}")
        if otype in ("LIMIT", "SL") and not (order.price and order.price > 0):
            raise ValueError(f"Flattrade {otype} order needs a positive price")
        if otype in ("SL", "SL-M") and not (order.trigger_price and order.trigger_price > 0):
            raise ValueError(f"Flattrade {otype} order needs a positive trigger_price")
        if (
            otype == "SL"
            and order.price is not None
            and order.trigger_price is not None
            and (
                (side == "B" and order.price < order.trigger_price)
                or (side == "S" and order.price > order.trigger_price)
            )
        ):
            raise ValueError(
                f"Flattrade SL {order.direction} limit {order.price} is on the wrong side "
                f"of trigger {order.trigger_price}"
            )
        return otype, prctyp, side, prd, (order.exchange or "NSE").upper()

    async def _order_fields(self, order: OrderRequest) -> dict[str, str]:
        """Validated wire fields shared by PlaceOrder and GetBasketMargin:
        exch, tsym, qty, prd, trantype, prctyp, prc (+ trgprc for SL-LMT only).

        Rounding: LIMIT never worse than asked (BUY down, SELL up); MARKET priced off
        the opposite side of the touch (see _marketable_price), clamped inside the
        quote's circuit limits; SL trigger to the nearest tick and its limit toward
        execution (BUY up, SELL down); SL-M limit = trigger*(1+band) UP for BUY,
        trigger*(1-band) DOWN for SELL.
        """
        otype, prctyp, side, prd, exch = self._validate(order)
        resolved = await self._symbols.resolve(f"{exch}:{order.symbol}")
        buy = side == "B"
        quote = await self._quote(resolved) if otype == "MARKET" else None
        tick = await self._tick(resolved)
        fields = {
            "exch": resolved.exchange,
            "tsym": resolved.tradingsymbol,
            "qty": str(order.quantity),
            "prd": prd,
            "trantype": side,
            "prctyp": prctyp,
        }
        price = order.price or 0.0  # presence checked by _validate
        if otype == "LIMIT":
            prc = orders.round_to_tick(price, tick, "down" if buy else "up")
        elif quote is not None:  # MARKET
            prc = self._marketable_price(quote, buy, tick, order.symbol)
        else:
            trg = orders.round_to_tick(order.trigger_price or 0.0, tick, "nearest")
            if otype == "SL":
                prc = orders.round_to_tick(price, tick, "up" if buy else "down")
            elif buy:
                prc = orders.round_to_tick(trg * (1 + self._band), tick, "up")
            else:
                # the lowest valid price is one tick: a sub-tick trigger still sells
                prc = max(orders.round_to_tick(trg * (1 - self._band), tick, "down"), tick)
            if trg <= 0:
                raise ValueError(f"Flattrade {otype} {order.symbol}: trigger rounds to zero")
            fields["trgprc"] = orders.fmt(trg)
        if prc <= 0:
            raise ValueError(f"Flattrade {otype} {order.symbol}: price {price} rounds to zero")
        fields["prc"] = orders.fmt(prc)
        return fields

    def _marketable_price(
        self, quote: dict[str, Any], buy: bool, tick: Decimal, symbol: str
    ) -> Decimal:
        """MARKET -> marketable limit priced off the opposite side of the touch.

        BUY: reference = max(LTP, best ask `sp1`), limit = reference*(1+band) UP.
        SELL: reference = min(LTP, best bid `bp1`), limit = reference*(1-band) DOWN.
        A missing / zero ask or bid falls back to LTP. The band therefore bounds the
        slippage beyond the price actually offered; place_order sends MARKET as IOC
        so an unfilled remainder is cancelled, never left resting.
        """
        ltp = Decimal(str(normalizer.to_float(quote.get("lp"))))
        if ltp <= 0:
            raise FlattradeError(f"Flattrade MARKET {symbol}: no LTP in GetQuotes; not priced")
        if buy:
            ask = Decimal(str(normalizer.to_float(quote.get("sp1"))))
            ref = max(ltp, ask)
            price = orders.round_to_tick(ref * (1 + self._band), tick, "up")
            upper = orders.parse_tick(quote.get("uc"))
            if upper is not None and price > upper:
                price = orders.round_to_tick(upper, tick, "down")
        else:
            bid = Decimal(str(normalizer.to_float(quote.get("bp1"))))
            ref = min(ltp, bid) if bid > 0 else ltp
            price = max(orders.round_to_tick(ref * (1 - self._band), tick, "down"), tick)
            lower = orders.parse_tick(quote.get("lc"))
            if lower is not None and price < lower:
                price = orders.round_to_tick(lower, tick, "up")
        return price

    async def place_order(self, order: OrderRequest) -> str:
        """PlaceOrder -> `norenordno`.

        MARKET is sent as an IOC marketable LIMIT in the regular session (fills now or
        is cancelled - check the fill, it is NOT guaranteed); an AMO keeps the
        requested validity. `remarks` is the tag prefix plus a per-attempt unique id.

        Raises:
            FlattradeLiveOrdersDisabled: capability live_orders_verified is false
                (the default) - nothing is sent.
            OrderWindowClosed: no order window open now (nothing sent).
            ValueError / KeyError: invalid request / unknown symbol (nothing sent).
            FlattradeError: account not enabled, broker rejection, or an ambiguous
                placement that matched several orders.
            FlattradeTransportError: transport failure; when `ambiguous`, the order
                book showed no matching order - the caller decides, nothing re-sent.
        """
        _require_live_orders("place_order")
        now = _now()
        variety = _order_variety(now)
        if variety not in (sessions.REGULAR, sessions.AMO):
            raise OrderWindowClosed(
                f"no order window open at {now:%H:%M} IST (see config/market_hours.json)"
            )
        otype, prctyp, _, prd, exch = self._validate(order)
        await self._ensure_enabled(prctyp, prd, exch)
        ret = order.validity.upper()
        if otype == "MARKET" and variety == sessions.REGULAR:
            ret = "IOC"  # a "market" order must never rest on the book as a limit
        payload: dict[str, Any] = {
            **await self._order_fields(order),
            "dscqty": "0",
            "ret": ret,
            "remarks": _unique_remarks(order.tag or order.agent_trade_id),
            "ordersource": "API",
        }
        if variety == sessions.AMO:
            payload["amo"] = "Yes"  # any other value is rejected ("Invalid AMO", A7)
        _log.info(
            "flattrade_place_order symbol=%s exch=%s side=%s qty=%s prctyp=%s prc=%s trgprc=%s "
            "prd=%s amo=%s remarks=%s",
            payload["tsym"],
            payload["exch"],
            payload["trantype"],
            payload["qty"],
            payload["prctyp"],
            payload["prc"],
            payload.get("trgprc", ""),
            payload["prd"],
            "amo" in payload,
            payload["remarks"],
        )
        since = _now()
        try:
            reply = await self._client.call("PlaceOrder", payload)
        except FlattradeTransportError as exc:
            if not exc.ambiguous:
                raise
            order_id = await self._resolve_ambiguous_place(payload, since, exc)
        else:
            order_id = str(reply.get("norenordno") or "") if isinstance(reply, dict) else ""
            if not order_id:
                raise FlattradeError(f"PlaceOrder: Ok reply without norenordno for {order.symbol}")
        self._placed_ids.add(order_id)
        return order_id

    async def _resolve_ambiguous_place(
        self, payload: dict[str, Any], since: datetime, exc: FlattradeTransportError
    ) -> str:
        """After an ambiguous PlaceOrder, find the order in the OrderBook instead of
        re-sending it. A row matches only on this attempt's unique `remarks` plus
        tsym + exch + side + qty + prctyp, was not already returned by this adapter,
        and has a parseable entry time inside [attempt - skew, lookup + skew].

        The book is read up to _AMBIGUOUS_POLLS times (the OMS may list the order a
        moment late). One match -> its norenordno. None after the last read ->
        re-raise `exc` (ambiguous: the caller must not blindly re-place). Several ->
        FlattradeError (reconcile manually). Never places again.
        """
        _log.warning(
            "flattrade_place_ambiguous tsym=%s remarks=%s - reading OrderBook before any retry",
            payload["tsym"],
            payload["remarks"],
        )
        for attempt in range(1, _AMBIGUOUS_POLLS + 1):
            try:
                rows = await self._book("OrderBook", {})
            except Exception as lookup_exc:
                _log.error("flattrade_place_ambiguous_lookup_failed err=%s", lookup_exc)
                raise exc from lookup_exc
            candidates = self._ambiguous_candidates(rows, payload, since)
            if len(candidates) == 1:
                _log.warning(
                    "flattrade_place_ambiguous_resolved norenordno=%s read=%d",
                    candidates[0],
                    attempt,
                )
                return candidates[0]
            if candidates:
                raise FlattradeError(
                    f"PlaceOrder outcome unknown and {len(candidates)} matching orders found "
                    f"({', '.join(candidates)}); reconcile manually - nothing was re-sent"
                ) from exc
            if attempt < _AMBIGUOUS_POLLS:
                await asyncio.sleep(_AMBIGUOUS_POLL_DELAY_S)
        _log.error(
            "flattrade_place_ambiguous_not_found tsym=%s remarks=%s reads=%d",
            payload["tsym"],
            payload["remarks"],
            _AMBIGUOUS_POLLS,
        )
        raise exc

    def _ambiguous_candidates(
        self, rows: list[dict[str, Any]], payload: dict[str, Any], since: datetime
    ) -> list[str]:
        """Order-book rows that are this exact attempt (see _resolve_ambiguous_place)."""
        earliest = since - _AMBIGUOUS_CLOCK_SKEW
        latest = _now() + _AMBIGUOUS_CLOCK_SKEW
        candidates: list[str] = []
        for row in rows:
            oid = str(row.get("norenordno") or "")
            if not oid or oid in self._placed_ids:
                continue
            same = (
                str(row.get("remarks") or "") == payload["remarks"]
                and str(row.get("tsym") or "") == payload["tsym"]
                and str(row.get("exch") or "").upper() == payload["exch"]
                and str(row.get("trantype") or "").upper() == payload["trantype"]
                and str(row.get("prctyp") or "").upper() == payload["prctyp"]
                and int(normalizer.to_float(row.get("qty"))) == int(payload["qty"])
            )
            if not same:
                continue
            entered = normalizer.parse_order_time(row.get("ordenttm")) or (
                normalizer.parse_order_time(row.get("norentm"))
            )
            if entered is None:
                _log.error(
                    "flattrade_place_ambiguous_unparseable_time norenordno=%s ordenttm=%r - "
                    "not accepted; reconcile manually",
                    oid,
                    row.get("ordenttm"),
                )
                continue
            if earliest <= entered <= latest:
                candidates.append(oid)
        return candidates

    async def _book(self, endpoint: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """A list endpoint: array -> rows; `"No Data"` -> []; any other shape raises."""
        try:
            reply = await self._client.call(endpoint, payload)
        except FlattradeError as exc:
            if _is_no_data(exc, endpoint):
                return []
            raise
        if not isinstance(reply, list) or not all(isinstance(r, dict) for r in reply):
            raise FlattradeError(f"{endpoint}: expected an array, got {type(reply).__name__}")
        return reply

    async def _history(self, order_id: str) -> list[dict[str, Any]]:
        """SingleOrdHist rows; an unknown order (empty / "No Data") raises KeyError."""
        rows = await self._book("SingleOrdHist", {"norenordno": order_id})
        if not rows:
            raise KeyError(f"Order {order_id!r} not found in today's order history")
        return rows

    @staticmethod
    def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
        """The current state from SingleOrdHist rows (one per report, `rpt`).

        Which end of the array is newest is UNVERIFIED (the brief assumed last;
        Noren clients commonly return newest FIRST), so the order is DETECTED:
        the creation report (`rpt` PendingNew / NewAck / New) is the oldest row;
        failing that, `norentm` of the first vs last row. Rows reporting a rejected
        MODIFY / CANCEL (`rpt` containing "rej" other than "Rejected") do not change
        the order's state and are skipped.

        Then: a terminal row wins wherever it sits (terminal states are final; two
        different ones raise); otherwise the newest row. When the order cannot be
        detected, rows that disagree on qty / prc / trgprc raise FlattradeError
        (never act on a possibly stale price); if they agree the most-filled row
        wins (fills only grow), ties -> the last one.
        """
        state_rows = [r for r in rows if not _is_amend_rejection(r.get("rpt"))] or rows
        statuses = [normalizer.normalize_status(r.get("status")) for r in state_rows]
        terminal = {s for s in statuses if s in normalizer.TERMINAL_STATUSES}
        if len(terminal) > 1:
            raise FlattradeError(
                f"SingleOrdHist: conflicting terminal statuses {sorted(terminal)} for "
                f"{rows[-1].get('norenordno')}"
            )
        newest_first = _history_newest_first(state_rows)
        if terminal:
            idxs = [i for i, s in enumerate(statuses) if s in terminal]
            return state_rows[idxs[0] if newest_first else idxs[-1]]
        if newest_first is not None:
            return state_rows[0] if newest_first else state_rows[-1]
        keys = {
            tuple(normalizer.to_float(r.get(k)) for k in ("qty", "prc", "trgprc"))
            for r in state_rows
        }
        if len(keys) > 1:
            raise FlattradeError(
                f"SingleOrdHist: cannot tell the newest of {len(state_rows)} rows with "
                f"different qty/prc/trgprc for {rows[-1].get('norenordno')} (no rpt / "
                "norentm ordering); not guessing"
            )
        best = max(
            range(len(state_rows)),
            key=lambda i: (normalizer.to_float(state_rows[i].get("fillshares")), i),
        )
        return state_rows[best]

    async def modify_order(self, order_id: str, params: dict[str, Any]) -> str:
        """ModifyOrder with the full field set (findings A3): exch, norenordno, tsym,
        prctyp, prc, qty (TOTAL order quantity, not pending), ret, and trgprc for
        SL-LMT only. Recognised params (Kite vocabulary): price, trigger_price,
        quantity, order_type (LIMIT / SL), validity. The current state comes from
        SingleOrdHist; a terminal order raises without sending. Changing only the
        trigger of an SL-LMT moves its limit by the same amount (keeps the band /
        the caller's original offset).
        """
        _require_live_orders("modify_order")
        unknown = set(params) - {"price", "trigger_price", "quantity", "order_type", "validity"}
        if unknown:
            raise ValueError(f"Flattrade modify_order: unsupported params {sorted(unknown)}")
        current = self._latest(await self._history(order_id))
        status = normalizer.normalize_status(current.get("status"))
        if status in normalizer.TERMINAL_STATUSES:
            raise FlattradeError(f"cannot modify order {order_id}: terminal status {status}")

        prctyp = str(current.get("prctyp") or "").upper()
        if "order_type" in params:
            new_type = {"LIMIT": "LMT", "SL": "SL-LMT"}.get(str(params["order_type"]).upper())
            if new_type is None:
                raise ValueError(
                    f"Flattrade modify_order: order_type {params['order_type']!r} not supported "
                    "(LIMIT or SL only)"
                )
            prctyp = new_type
        buy = str(current.get("trantype") or "").upper() == "B"
        qty = int(params.get("quantity", normalizer.to_float(current.get("qty"))))
        if qty <= 0:
            raise ValueError(f"Flattrade modify_order: quantity must be positive, got {qty}")
        ret = str(params.get("validity") or current.get("ret") or "DAY").upper()
        if ret not in orders.VALIDITIES:
            raise ValueError(f"Flattrade validity {ret!r} not supported")
        tick = orders.parse_tick(current.get("ti")) or orders.DEFAULT_TICK
        old_prc = Decimal(str(normalizer.to_float(current.get("prc"))))

        payload: dict[str, Any] = {
            "exch": str(current.get("exch") or ""),
            "norenordno": order_id,
            "tsym": str(current.get("tsym") or ""),
            "prctyp": prctyp,
            "qty": str(qty),
            "ret": ret,
        }
        if prctyp == "LMT":
            if "trigger_price" in params:
                raise ValueError("Flattrade modify_order: trigger_price on a LIMIT order")
            prc = Decimal(str(params["price"])) if "price" in params else old_prc
            payload["prc"] = orders.fmt(orders.round_to_tick(prc, tick, "down" if buy else "up"))
        elif prctyp == "SL-LMT":
            old_trg = Decimal(str(normalizer.to_float(current.get("trgprc"))))
            trg = Decimal(str(params["trigger_price"])) if "trigger_price" in params else old_trg
            if trg <= 0:
                raise ValueError("Flattrade modify_order: SL-LMT needs a positive trigger_price")
            if "price" in params:
                prc = Decimal(str(params["price"]))
            elif "trigger_price" in params and old_trg > 0:
                prc = old_prc + (trg - old_trg)
            else:
                prc = old_prc
            if prc <= 0 or (buy and prc < trg) or (not buy and prc > trg):
                raise ValueError(
                    f"Flattrade modify_order: SL limit {prc} on the wrong side of trigger {trg}"
                )
            payload["trgprc"] = orders.fmt(orders.round_to_tick(trg, tick, "nearest"))
            payload["prc"] = orders.fmt(orders.round_to_tick(prc, tick, "up" if buy else "down"))
        else:
            raise FlattradeError(f"cannot modify order {order_id} with prctyp {prctyp!r}")

        try:
            reply = await self._client.call("ModifyOrder", payload)
        except FlattradeTransportError as exc:
            if not exc.ambiguous:
                raise
            return await self._confirm_modify(order_id, payload, exc)
        if isinstance(reply, dict) and reply.get("result"):
            return str(reply["result"])
        return order_id

    async def _confirm_modify(
        self, order_id: str, payload: dict[str, Any], exc: FlattradeTransportError
    ) -> str:
        """After an ambiguous ModifyOrder: re-read the order and report the truth."""
        try:
            latest = self._latest(await self._history(order_id))
        except Exception as lookup_exc:
            raise exc from lookup_exc
        applied = all(
            normalizer.to_float(latest.get(k)) == normalizer.to_float(payload[k])
            for k in ("qty", "prc", "trgprc")
            if k in payload
        )
        if applied:
            _log.warning("flattrade_modify_ambiguous_applied norenordno=%s", order_id)
            return order_id
        raise FlattradeError(
            f"ModifyOrder outcome unknown for {order_id}: broker shows "
            f"status={latest.get('status')} qty={latest.get('qty')} prc={latest.get('prc')} "
            f"trgprc={latest.get('trgprc')} (requested qty={payload['qty']} prc={payload['prc']} "
            f"trgprc={payload.get('trgprc')})"
        ) from exc

    async def cancel_order(self, order_id: str) -> bool:
        """CancelOrder; True once sent. After an ambiguous failure, True only if the
        order history shows it CANCELLED, otherwise FlattradeError with the state."""
        _require_live_orders("cancel_order")
        try:
            await self._client.call("CancelOrder", {"norenordno": order_id})
        except FlattradeTransportError as exc:
            if not exc.ambiguous:
                raise
            try:
                latest = self._latest(await self._history(order_id))
            except Exception as lookup_exc:
                raise exc from lookup_exc
            status = normalizer.normalize_status(latest.get("status"))
            if status == "CANCELLED":
                _log.warning("flattrade_cancel_ambiguous_confirmed norenordno=%s", order_id)
                return True
            raise FlattradeError(
                f"CancelOrder outcome unknown for {order_id}: broker shows status={status}"
            ) from exc
        return True

    async def get_order_status(self, order_id: str) -> Order:
        """Current state from SingleOrdHist (see _latest); unknown order -> KeyError."""
        return normalizer.normalize_order(self._latest(await self._history(order_id)))

    async def get_all_orders(self) -> list[Order]:
        """Today's OrderBook; "No Data" -> []."""
        return [normalizer.normalize_order(r) for r in await self._book("OrderBook", {})]

    async def get_trades(self, order_id: str) -> list[Fill]:
        """TradeBook fills of one order (`fillshares`/partials appear as several rows)."""
        rows = await self._book("TradeBook", {})
        return [normalizer.normalize_fill(r) for r in rows if str(r.get("norenordno")) == order_id]

    async def get_order_margin(self, orders_: list[OrderRequest]) -> OrderMarginResult:
        """GetBasketMargin: the first order's fields at top level plus `basketlists`
        for the rest; `marginused` -> total_margin, per_leg [], raw = the reply.

        GATED: raises NotImplementedError (before any validation or request) unless
        capability_matrix.json flattrade.margin_api_verified is true (default false).
        UNVERIFIED (findings C-9): whether the basket figure nets the hedge benefit
        of a credit spread, and whether a non-empty `remarks` can accompany a usable
        figure - any `remarks` is treated as a rejection here.
        """
        if not _CAPS.get("margin_api_verified", False):
            raise NotImplementedError(
                "Flattrade GetBasketMargin is implemented but UNVERIFIED (hedge netting, "
                "findings C-9); set capability margin_api_verified=true after a "
                "paper-basket check"
            )
        if not orders_:
            raise ValueError("get_order_margin: no orders to price")
        legs = [await self._order_fields(o) for o in orders_]
        payload: dict[str, Any] = {**legs[0], "basketlists": legs[1:]}
        reply = await self._client.call("GetBasketMargin", payload)
        if not isinstance(reply, dict):
            raise FlattradeError("GetBasketMargin: unexpected response shape")
        remarks = str(reply.get("remarks") or "").strip()
        if remarks:
            raise FlattradeError(f"GetBasketMargin rejected the basket: {remarks}")
        if reply.get("marginused") in (None, ""):
            raise FlattradeError("GetBasketMargin: reply has no marginused")
        return OrderMarginResult(
            total_margin=normalizer.to_float(reply["marginused"]), per_leg=[], raw=reply
        )

    # ── Positions & portfolio (F3) ─────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """PositionBook (array, or "No Data" -> []); `lp` and MTM come in the rows."""
        return [normalizer.normalize_position(r) for r in await self._book("PositionBook", {})]

    async def get_holdings(self) -> list[Holding]:
        """Holdings (`prd: "C"`), priced with one GetQuotes per holding (the reply has
        no LTP in the findings). A refused quote leaves last_price/pnl at 0. The
        quantity mapping is UNVERIFIED (see normalizer.normalize_holding)."""
        rows = await self._book("Holdings", {"prd": "C"})
        if rows and not self._warned_holdings_qty:
            self._warned_holdings_qty = True
            _log.warning(
                "flattrade_holdings_qty_unverified - quantity=dpqty-usedqty, "
                "t1_quantity=holdqty+btstqty (findings list names only); verify against "
                "the Flattrade back office before trusting reconciliation"
            )
        scrips: dict[str, Resolved] = {}
        for i, row in enumerate(rows):
            try:
                scrip = normalizer.holding_scrip(row)
            except ValueError as exc:
                raise FlattradeError(f"Holdings: {exc}") from exc
            if scrip.get("token"):
                scrips[str(i)] = Resolved(
                    str(scrip["token"]),
                    str(scrip.get("exch") or "").upper(),
                    str(scrip.get("tsym")),
                )
        quotes = await self._quotes(scrips) if scrips else {}
        return [
            normalizer.normalize_holding(row, normalizer.to_float(quotes.get(str(i), {}).get("lp")))
            for i, row in enumerate(rows)
        ]

    async def get_margins(self) -> Margin:
        """Limits -> Margin (formula in normalizer.margin_from_limits)."""
        reply = await self._client.call("Limits", {})
        if not isinstance(reply, dict):
            raise FlattradeError("Limits: unexpected response shape")
        try:
            return normalizer.margin_from_limits(reply)
        except ValueError as exc:
            raise FlattradeError(f"Limits: {exc}") from exc

    # ── Protected orders: GTT / OCO UNVERIFIED (findings A4) ──────────────────

    async def place_gtt(self, gtt: GTTRequest) -> str:
        raise NotImplementedError(_GTT_UNVERIFIED)

    async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str:
        raise NotImplementedError(_GTT_UNVERIFIED)

    async def cancel_gtt(self, gtt_id: str) -> bool:
        raise NotImplementedError(_GTT_UNVERIFIED)

    async def get_all_gtts(self) -> list[GTTOrder]:
        raise NotImplementedError(_GTT_UNVERIFIED)

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
