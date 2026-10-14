"""
USS — Universal Strategy Spec schema.

All strategy formats (JSON, YAML, Pine, NL) compile to USSSpec.
The engine only ever executes USSSpec — never raw input formats.

Required fields (validator enforces): stop_loss, target.
Safety invariants (validator enforces, never configurable):
  - stop_loss always present
  - stop_loss max_sl_pct <= operator hard limit
  - R:R >= operator hard limit
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Product(StrEnum):
    DELIVERY = "DELIVERY"
    MIS = "MIS"
    NRML = "NRML"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"  # strategy fires long breakouts and short breakdowns (e.g. ORB)


class Timeframe(StrEnum):
    TICK = "TICK"
    ONE_MIN = "1MIN"
    FIVE_MIN = "5MIN"
    FIFTEEN_MIN = "15MIN"
    THIRTY_MIN = "30MIN"
    HOURLY = "1HOUR"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class ConditionType(StrEnum):
    PRICE = "PRICE"
    INDICATOR = "INDICATOR"
    VOLUME = "VOLUME"
    PATTERN = "PATTERN"
    COMPOSITE = "COMPOSITE"


class LogicOperator(StrEnum):
    AND = "AND"
    OR = "OR"


class SLMethod(StrEnum):
    PATTERN_LEVEL = "pattern_level"
    ATR_BASED = "atr_based"
    FIXED_PCT = "fixed_pct"
    FIXED_POINTS = "fixed_points"
    RECENT_LOW = "recent_low"
    RECENT_HIGH = "recent_high"
    ORB_RANGE = "orb_range"  # SL at range_fraction × opening-range width from entry
    ENTRY_CANDLE = "entry_candle"  # SL at the low/high of the entry (breakout) candle itself
    PULLBACK_LOW = "pullback_low"  # SL at the tracked pullback low (from a
    # `pullback_trigger` PATTERN condition) minus atr_multiplier × ATR(atr_period)


class TargetMethod(StrEnum):
    RISK_REWARD = "risk_reward"
    FIXED_PCT = "fixed_pct"
    FIXED_POINTS = "fixed_points"
    INDICATOR = "indicator"


class TrailMethod(StrEnum):
    INDICATOR_CROSS = "indicator_cross"
    ATR_TRAIL = "atr_trail"
    CHANDELIER = "chandelier"
    FIXED_PCT = "fixed_pct"
    SMA_BAND = "sma_band"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class USSMeta(BaseModel):
    name: str
    display_name: str = ""
    version: str = "1.0"
    product: Product = Product.DELIVERY
    direction: Direction = Direction.LONG
    timeframe: Timeframe = Timeframe.DAILY

    @model_validator(mode="after")
    def _fill_display_name(self) -> USSMeta:
        if not self.display_name:
            self.display_name = self.name.replace("_", " ").title()
        return self


class UniverseFilter(BaseModel):
    min_price: float = 0.0
    max_price: float | None = None
    min_adv_crores: float = 0.0
    # Applied at the live swing scan only (latest fundamentals_snapshot); backtests
    # can't apply it — there is no historical market cap. Unknown cap = kept.
    max_market_cap_crores: float | None = None
    exchanges: list[str] = Field(default_factory=lambda: ["NSE"])
    # One or more index universes (e.g. ["NIFTY50", "BANKNIFTY"]). A bare
    # string is accepted and normalized to a single-element list for back-compat.
    index_filter: list[str] | None = None
    # Explicit symbol whitelist (bare names, e.g. "BHARTIARTL"). None = no restriction.
    symbols: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_index_filter(cls, data: Any) -> Any:
        if isinstance(data, dict):
            idx = data.get("index_filter")
            if isinstance(idx, str):
                data["index_filter"] = [idx]
        return data


class Condition(BaseModel):
    name: str
    type: ConditionType
    # Toggle a condition off without removing it (e.g. the ORB volume filter).
    enabled: bool = True
    # Flexible params — interpreted by ConditionCompiler per type
    params: dict[str, Any] = Field(default_factory=dict)


class EntryConditions(BaseModel):
    logic: LogicOperator = LogicOperator.AND
    conditions: list[Condition]


class StopLoss(BaseModel):
    method: SLMethod
    # Method-specific params
    reference: str | None = None  # e.g. "consolidation_low"
    buffer_pct: float = 0.0
    max_sl_pct: float = 10.0  # hard cap; validator checks vs operator limit
    min_sl_pct: float = 0.0  # floor; 0 = no-op. Prevents structural methods (e.g.
    # entry_candle) from collapsing to a near-zero stop on low-volatility candles,
    # where gap-slippage + charges alone can exceed the intended risk.
    atr_period: int | None = None
    atr_multiplier: float | None = None
    fixed_pct: float | None = None
    fixed_points: float | None = None
    lookback_days: int | None = None
    range_fraction: float | None = None  # for orb_range: SL = entry ± fraction × OR width


class Target(BaseModel):
    method: TargetMethod
    ratio: float | None = None  # for RISK_REWARD
    fixed_pct: float | None = None
    fixed_points: float | None = None
    indicator: str | None = None
    period: int | None = None


class TrailingStop(BaseModel):
    method: TrailMethod
    indicator: str | None = None
    period: int | None = None
    cross: str | None = None  # e.g. "price_below_on_close"
    atr_period: int | None = None
    atr_multiplier: float | None = None
    chandelier_period: int | None = None
    chandelier_multiplier: float | None = None
    fixed_pct: float | None = None


class GivebackLock(BaseModel):
    """Profit ratchet: once max favourable excursion (MFE) reaches `activate_mfe_r`,
    the stop is held at entry + `lock_fraction` × MFE and follows MFE outward.
    Caps what a green trade can give back without capping its upside."""

    activate_mfe_r: float = Field(gt=0)
    lock_fraction: float = Field(gt=0, lt=1)


class TimeDecayStep(BaseModel):
    """From clock time `at` (HH:MM, exchange-local bar time) the stop is tightened to
    entry + `lock_r` × initial risk (negative = a smaller loss, 0 = breakeven).
    Applies only when MFE so far is >= `min_mfe_r` (None = applies to every trade)."""

    at: str = Field(pattern=r"^\d{2}:\d{2}$")
    lock_r: float
    min_mfe_r: float | None = None


class ExitConditions(BaseModel):
    trailing_stop: TrailingStop | None = None
    time_stop_days: int | None = None
    additional: list[Condition] = Field(default_factory=list)
    # --- Intraday exit-model controls (None/False => feature off) ---
    # Move the stop to breakeven once the trade is this many R in profit.
    breakeven_at_r: float | None = None
    # Book 50% of the position at this many R, then ride the rest risk-free.
    partial_book_r: float | None = None
    # Run-up (R) the trade must reach before the trailing stop engages. 0.0 =
    # trail from entry; None = engine default (1.5R intraday, 1.0R positional).
    trail_activation_r: float | None = None
    # Heikin-Ashi / EMA-9 reversal guard on the remaining position.
    reversal_exit: bool = False
    # Only fire the time stop while still below trail_activation_r run-up —
    # False (default) preserves the existing unconditional behavior every
    # current strategy relies on.
    time_stop_before_activation: bool = False
    # Exit the whole position when `regime_filter`'s long/short gate flips
    # against it (requires `USSSpec.regime_filter` to be set).
    regime_flip_exit: bool = False
    # Fraction of the position booked at partial_book_r/breakeven_at_r.
    # 0.5 preserves the existing hardcoded 50/50 split.
    partial_fraction: float = 0.5
    # Tighten-only profit ratchet and session-clock stop decay (None/[] => off).
    giveback_lock: GivebackLock | None = None
    time_decay: list[TimeDecayStep] = Field(default_factory=list)


class MarketFilter(BaseModel):
    """Broad-market direction gate — ORB.md's request.security(NSE:NIFTY).

    mode "daily_direction" (default): longs allowed only while the benchmark
    trades above its previous-day close; shorts only while below.
    mode "regime_sma": longs allowed only while the benchmark closes above its
    `sma_period`-bar SMA (O'Neil's "M" — market in a confirmed uptrend); shorts
    mirror. Suited to swing strategies where day-direction is too twitchy.
    `symbol` is the benchmark index (e.g. "NIFTY 50", "NIFTY BANK"); it follows
    whatever the operator configures and is never hardwired in engine logic.
    mode "breadth": longs allowed only while the fraction of the stock universe
    closing above its own `sma_period`-day SMA is >= `min_breadth` (cross-sectional,
    supplied via CompiledStrategy.set_breadth_context; `symbol` is unused).
    """

    symbol: str = "NIFTY 50"
    enabled: bool = True
    mode: Literal["daily_direction", "regime_sma", "breadth"] = "daily_direction"
    sma_period: int = 200
    min_breadth: float = Field(default=0.5, ge=0.0, le=1.0)


class RegimeFilter(BaseModel):
    """Self-referential regime gate — the traded symbol's OWN price vs its own
    long SMA, with a slope requirement (the SMA must sit above where it was
    `slope_lookback_bars` ago). Unlike MarketFilter (a benchmark-index gate
    shared across a whole strategy scan), this reads the strategy's own df,
    so it is computed per-symbol like any other entry condition.
    """

    enabled: bool = True
    sma_period: int = 200
    slope_lookback_bars: int = 20
    # Consecutive stop-outs allowed within one continuous in-regime phase
    # before CRM blocks further entries for this strategy+symbol (resets the
    # instant the regime gate goes false). Enforced by CRM, not the compiler.
    max_attempts_per_phase: int = 3


class IntradaySession(BaseModel):
    """Opening-Range-Breakout session windows (HHMM-HHMM, local exchange time).

    range_window : bars whose high/low define the day's opening range
    entry_window : entries are only allowed inside this window
    exit_window  : any open position is force-flattened here (EOD square-off)
    """

    range_window: str = "0915-0920"
    entry_window: str = "0920-1500"
    exit_window: str = "1510-1530"
    timezone: str = "Asia/Kolkata"

    @model_validator(mode="after")
    def _within_market_hours(self) -> IntradaySession:
        """A strategy may only NARROW the market's times (config/market_hours.json),
        never exceed them: range/exit inside the regular session, entries no later
        than the EXIT_ONLY window start."""
        from src.market import sessions

        def _bounds(w: str) -> tuple[int, int]:
            a, b = w.split("-")
            return int(a[:2]) * 60 + int(a[2:]), int(b[:2]) * 60 + int(b[2:])

        lo = sessions.minutes(sessions.regular_open())
        hi = sessions.minutes(sessions.regular_close())
        entry_hi = sessions.minutes(sessions.exit_only_start())
        for name, w, limit in (
            ("range_window", self.range_window, hi),
            ("entry_window", self.entry_window, entry_hi),
            ("exit_window", self.exit_window, hi),
        ):
            start, end = _bounds(w)
            if start < lo or end > limit or start >= end:
                raise ValueError(
                    f"{name} {w} outside market hours {lo // 60:02d}{lo % 60:02d}-"
                    f"{limit // 60:02d}{limit % 60:02d} (config/market_hours.json)"
                )
        return self


# ---------------------------------------------------------------------------
# Top-level spec
# ---------------------------------------------------------------------------


class USSSpec(BaseModel):
    """Universal Strategy Spec — the single internal representation."""

    meta: USSMeta
    universe_filter: UniverseFilter = Field(default_factory=UniverseFilter)
    entry_conditions: EntryConditions
    stop_loss: StopLoss
    target: Target
    exit_conditions: ExitConditions = Field(default_factory=ExitConditions)
    intraday: IntradaySession | None = None  # set → ORB session-window semantics
    market_filter: MarketFilter | None = None  # set → broad-market direction gate
    regime_filter: RegimeFilter | None = None  # set → self-referential regime gate
    # Per-strategy risk-per-trade override, as % of account equity. CRM (G8)
    # may only TIGHTEN the global rupee risk cap with this — never loosen it.
    risk_pct: float | None = None

    # Runtime metadata (set by registry, not user)
    source_format: Literal["json", "yaml", "pine", "nl", "llm"] = "json"
    enabled: bool = True
