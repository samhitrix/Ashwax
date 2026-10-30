"""
Canonical broker models and BrokerInterface ABC.

No code outside src/brokers/ ever imports broker-specific APIs.
All components use these canonical types exclusively.

Product codes are always canonical here:
  INTRADAY | DELIVERY | MARGIN
Each adapter translates to its broker's codes (MIS, CNC, NRML, I, D, …).

Order statuses are always canonical:
  OPEN | PARTIAL | FILLED | CANCELLED | REJECTED
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


# ── Canonical models ──────────────────────────────────────────────────────────


@dataclass
class Fill:
    """One execution fill (partial or full)."""

    order_id: str
    trade_id: str
    quantity: int
    price: float
    exchange_time: datetime


@dataclass
class OrderRequest:
    """What we send to the broker to place an order."""

    symbol: str
    exchange: str  # "NSE" | "BSE" | "NFO" | "MCX"
    direction: str  # "BUY" | "SELL"
    quantity: int
    order_type: str  # "MARKET" | "LIMIT" | "SL" | "SL-M"
    product: str  # "INTRADAY" | "DELIVERY" | "MARGIN"
    validity: str  # "DAY" | "IOC"
    agent_trade_id: str
    signal_id: str
    tag: str = ""
    price: float | None = None
    trigger_price: float | None = None


@dataclass
class Order:
    """Canonical order state as returned or updated by the broker."""

    broker_order_id: str
    symbol: str
    direction: str  # "BUY" | "SELL"
    quantity: int
    filled_quantity: int
    pending_quantity: int
    status: str  # "OPEN" | "PARTIAL" | "FILLED" | "CANCELLED" | "REJECTED"
    order_type: str
    product: str  # "INTRADAY" | "DELIVERY" | "MARGIN"
    average_price: float
    tag: str
    placed_at: datetime
    fills: list[Fill] = field(default_factory=list)
    exchange_order_id: str = ""
    rejection_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tick:
    """One real-time market data tick."""

    symbol: str
    instrument_token: str
    ltp: float
    bid: float
    ask: float
    volume: int
    exchange_ts: datetime
    received_ts: datetime


@dataclass
class GTTRequest:
    """Good Till Triggered order placement request."""

    symbol: str
    product: str  # "INTRADAY" | "DELIVERY"
    gtt_type: str  # "SINGLE_LEG" | "TWO_LEG"
    quantity: int
    agent_trade_id: str
    sl_trigger: float
    sl_order_type: str  # "MARKET" | "LIMIT"
    sl_price: float | None = None
    target_trigger: float | None = None
    target_price: float | None = None


@dataclass
class GTTOrder:
    """GTT order state as returned by the broker."""

    gtt_id: str
    symbol: str
    status: str  # "ACTIVE" | "TRIGGERED" | "CANCELLED" | "EXPIRED"
    gtt_type: str
    sl_trigger: float
    target_trigger: float | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """Open intraday or overnight position."""

    symbol: str
    exchange: str
    direction: str  # "long" | "short"
    quantity: int
    average_price: float
    last_price: float
    pnl: float
    product: str  # "INTRADAY" | "DELIVERY" | "MARGIN"


@dataclass
class Holding:
    """Long-term CNC/delivery holding."""

    symbol: str
    exchange: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float
    # Shares bought T+1 day(s) ago that Zerodha hasn't moved into `quantity` yet —
    # still real, currently-held shares, just settlement-pending. `quantity` alone
    # reads 0 for a position in this state (found 2026-09-08: CGCL, filled the
    # previous trading day, sat here with quantity=0/t1_quantity=45 and was still
    # invisible to reconciliation, which only ever checked `quantity`).
    t1_quantity: int = 0


@dataclass
class Margin:
    """Account margin/funds summary. equity is the source of truth for capital."""

    equity: float  # total account value (cash + positions)
    available_cash: float
    used_margin: float
    collateral: float = 0.0
    # Margin usable for F&O: free cash + pledged collateral, net of margin already used. None when
    # the broker doesn't report it (fno_usable then falls back to available_cash).
    fno_available: float | None = None

    @property
    def fno_usable(self) -> float:
        """Margin an F&O order may draw on. available_cash alone is cash-only (right for CNC
        delivery) and wrongly ignores collateral, which the broker does accept for F&O."""
        return self.fno_available if self.fno_available is not None else self.available_cash


@dataclass
class OrderMarginResult:
    """Real pre-trade margin for a prospective multi-leg order, from the
    broker's margin-calculator endpoint — not account funds (see Margin).
    total_margin is the netted basket figure; per_leg is diagnostic only,
    never summed by callers (netting is the point of a basket call)."""

    total_margin: float
    per_leg: list[float] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BidAsk:
    price: float
    quantity: int
    orders: int


@dataclass
class MarketDepth:
    symbol: str
    bids: list[BidAsk]
    asks: list[BidAsk]
    ltp: float
    timestamp: datetime


@dataclass
class Instrument:
    symbol: str  # tradingsymbol, e.g. "RELIANCE24JUL2800CE"
    exchange: str
    instrument_token: str
    instrument_type: str  # "EQ" | "FUT" | "CE" | "PE"
    lot_size: int
    tick_size: float
    expiry: datetime | None = None
    strike: float | None = None
    name: str = ""  # underlying root, e.g. "RELIANCE" (NFO option chain filtering)


@dataclass
class OptionQuote:
    """A single option (or any derivative) quote with OI + volume.

    Used by the options structure-selection layer for liquidity checks and
    implied-vol computation. ltp/bid/ask feed premium + IV; oi/volume feed the
    liquidity_min_oi / liquidity_min_volume gates.
    """

    symbol: str
    ltp: float
    bid: float
    ask: float
    oi: int  # open interest (contracts)
    volume: int  # day volume (contracts)
    prev_oi: int = 0  # prior open interest, 0 if unavailable

    @property
    def mid(self) -> float:
        """Mid price; falls back to ltp when one side of the book is empty."""
        if self.bid > 0.0 and self.ask > 0.0:
            return (self.bid + self.ask) / 2.0
        return self.ltp


@dataclass
class SessionInfo:
    access_token: str
    user_id: str
    login_time: datetime
    expires_at: datetime | None = None


@dataclass
class SessionStatus:
    is_valid: bool
    user_id: str
    message: str = ""


class OrderWindowClosed(Exception):
    """An order was attempted while the market is in a no-order gap (between the
    regular-session close and the AMO open, or the AMO close and the session
    open — config/market_hours.json). Nothing is sent to the broker; the caller
    must hold the order until the window opens."""


@dataclass
class BrokerError:
    """Canonical broker error. Adapters map broker-specific codes to these."""

    code: str  # "MARGIN" | "CIRCUIT" | "PRODUCT" | "TOKEN" | "UNKNOWN"
    message: str
    retryable: bool
    original_code: str = ""
    original_message: str = ""


# ── BrokerInterface ABC ───────────────────────────────────────────────────────


class BrokerInterface(ABC):
    """
    Abstract base class for all broker adapters.

    Switching brokers = change one field in global.json (broker.name).
    No engine code changes. All components receive canonical models only.
    """

    # ── Authentication ────────────────────────────────────────────────────────

    @abstractmethod
    async def login(self) -> SessionInfo: ...

    @abstractmethod
    async def refresh_token(self) -> SessionInfo: ...

    @abstractmethod
    async def get_session_status(self) -> SessionStatus: ...

    # ── Market Data ───────────────────────────────────────────────────────────

    @abstractmethod
    async def subscribe_ticks(self, tokens: list[str]) -> None: ...

    @abstractmethod
    async def unsubscribe_ticks(self, tokens: list[str]) -> None: ...

    @abstractmethod
    async def get_ltp(self, symbols: list[str]) -> dict[str, float]: ...

    @abstractmethod
    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame: ...

    @abstractmethod
    async def get_depth(self, symbol: str) -> MarketDepth: ...

    @abstractmethod
    async def get_instruments(self, exchange: str) -> list[Instrument]: ...

    @abstractmethod
    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]: ...

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> str: ...  # returns order_id

    @abstractmethod
    async def modify_order(self, order_id: str, params: dict[str, Any]) -> str: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> Order: ...

    @abstractmethod
    async def get_all_orders(self) -> list[Order]: ...

    @abstractmethod
    async def get_trades(self, order_id: str) -> list[Fill]: ...

    @abstractmethod
    async def get_order_margin(self, orders: list[OrderRequest]) -> OrderMarginResult:
        """Real pre-trade margin for a prospective (not-yet-placed) basket of
        orders. Read-only — places nothing. Every implementation must return
        the broker's real margin figure or raise; never approximate."""
        ...

    # ── Positions & Portfolio ─────────────────────────────────────────────────

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    async def get_margins(self) -> Margin: ...

    # ── Protected Orders (GTT / Bracket) ─────────────────────────────────────

    @abstractmethod
    async def place_gtt(self, gtt: GTTRequest) -> str: ...  # returns gtt_id

    @abstractmethod
    async def modify_gtt(self, gtt_id: str, params: dict[str, Any]) -> str: ...

    @abstractmethod
    async def cancel_gtt(self, gtt_id: str) -> bool: ...

    @abstractmethod
    async def get_all_gtts(self) -> list[GTTOrder]: ...

    # ── Capability flags ──────────────────────────────────────────────────────

    @abstractmethod
    def supports_gtt(self) -> bool: ...

    @abstractmethod
    def supports_bracket_order(self) -> bool: ...

    @abstractmethod
    def supports_cover_order(self) -> bool: ...

    # ── Normalization (raw broker dict → canonical model) ─────────────────────

    @abstractmethod
    def normalize_tick(self, raw: dict[str, Any]) -> Tick: ...

    @abstractmethod
    def normalize_order(self, raw: dict[str, Any]) -> Order: ...

    @abstractmethod
    def normalize_position(self, raw: dict[str, Any]) -> Position: ...

    @abstractmethod
    def normalize_error(self, raw: dict[str, Any]) -> BrokerError: ...
