"""
Central Risk Manager (CRM) — 10-gate signal pipeline.

Gate pipeline (in order):
  G0  master_switch      global kill-switch
  G1  hard_caps          operator.json limits + 25% ABSOLUTE FLOOR (HARDCODED)
  G2  circuit_breakers   daily loss, consecutive losses
  G3  time_of_day        market session windows
  G4  regime             strategy allowed per market regime
  G5  news               news halt / earnings proximity
  G6  strategy_health    strategy scoring gate
  G7  portfolio_margin   position count + daily trade count
  G8  sl_sizing          risk-based sizing + HARD CAP FINAL CHECK
  G9  llm_confirmation   BLOCKING (REJECT or sub-floor confidence halts trade)
  G10 autonomy_mode      manual / assisted / autonomous routing

Each gate is a method returning GateResult. BLOCK stops the pipeline.
G9 blocks on REJECT or low confidence, but fails OPEN when no genuine LLM verdict
was produced (outage / no provider) so an LLM failure never halts trading.

The 25% absolute floor in G1 is HARDCODED — not configurable via JSON.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger
from src.market import sessions as _sessions
from src.risk.circuit_breaker import CircuitBreakerEngine
from src.risk.position_sizing import calculate_quantity

_log = get_logger("gates")
_circuit_breaker = CircuitBreakerEngine()

_IST = ZoneInfo("Asia/Kolkata")

from src.risk.time_filter import TimeFilter as _TimeFilter  # noqa: E402

_TIME_FILTER = _TimeFilter()

# Absolute equity floor: config/capital.json account.stop_all_trading_if_equity_falls_below
# (default 0.25, rejected if outside (0, 1)). Read once per process.
from src.risk.floor_config import absolute_floor_pct as _absolute_floor_pct  # noqa: E402

_ABSOLUTE_FLOOR_PCT: float = _absolute_floor_pct()

# Registry strategy names (signal.strategy_name) of the two desk-hosted strategies that
# own their own uniqueness guarantee (at most one concurrent position each -- see
# _g7_portfolio_margin's own comment on the exemption these names trigger). Must match
# options_settings.json options_trading.{options_expiry_day,options_intraday_short_premium,
# options_intraday_short_premium2}.registry_name exactly.
_HOSTED_OPTIONS_STRATEGIES: frozenset[str] = frozenset(
    {"options_expiry_day", "options_intraday_short_premium", "options_intraday_short_premium2"}
)
# options_intraday_short_premium runs EVERY trading day regardless of how many positions are open or how many
# trades were taken (operator decision 2026-10-12): exempt from the open-position count, the
# new-trades-per-day cap, the total-exposure cap and the portfolio open-risk cap in G7. It still
# faces every other gate (daily-loss caps, circuit breakers, regime, news, G9 when required...).
# Must match options_settings.json options_trading.options_intraday_short_premium{,2}.registry_name.
_UNCAPPED_OPTIONS_STRATEGIES: frozenset[str] = frozenset(
    {"options_intraday_short_premium", "options_intraday_short_premium2"}
)


_INTRADAY_TIMEFRAMES = frozenset({"5MIN", "15MIN", "5M", "15M"})


def engine_of(timeframe: str | None) -> str:
    """Classify a strategy timeframe into an execution engine bucket.

    Intraday = 5MIN/15MIN (the ORB family); everything else (DAILY/WEEKLY,
    unknown, empty) is swing. This single rule is the intraday/swing boundary
    used by both the budget gate and the per-engine counters.
    """
    return "intraday" if (timeframe or "").upper() in _INTRADAY_TIMEFRAMES else "swing"


def resolve_engine_counts(
    per_strategy: dict[str, int],
    name_to_engine: dict[str, str],
) -> tuple[int, int]:
    """Sum per-strategy trade counts into (intraday_total, swing_total).

    A strategy not present in name_to_engine is counted as swing — the more
    conservative bucket — so an unmapped name can never silently inflate the
    intraday budget.
    """
    intraday_total = 0
    swing_total = 0
    for name, count in per_strategy.items():
        if name_to_engine.get(name) == "intraday":
            intraday_total += int(count)
        else:
            swing_total += int(count)
    return intraday_total, swing_total


class GateDecision(StrEnum):
    PASS = "PASS"  # noqa: S105
    BLOCK = "BLOCK"
    MODIFY = "MODIFY"


@dataclass
class GateResult:
    gate_id: str
    decision: GateDecision
    reason: str = ""
    modified_quantity: int | None = None


@dataclass
class CRMSignal:
    """Inbound signal from the strategy layer."""

    signal_id: str
    symbol: str
    direction: str  # "BUY" | "SELL"
    strategy_name: str
    intended_quantity: int
    stop_loss_pct: float  # e.g. 0.02 for 2%
    target_pct: float  # e.g. 0.04 for 4%
    risk_rupees: float  # pre-computed by strategy
    entry_price: float = 0.0  # absolute entry price; required for G8 calculate_quantity path
    regime: str = "UNKNOWN"
    strategy_health: str = "UNKNOWN"
    news_halt: bool = False
    earnings_proximity_days: int = 999
    high_magnitude_news: bool = False  # HIGH magnitude news in last 2h → confidence -0.30
    rs_new_high: bool = False  # RS line at new 52-week high → confidence +0.10
    sector_declining: bool = False  # Symbol's sector in DECLINING rotation → block
    vix: float = 15.0  # current India VIX; populated by orchestrator _regime_loop
    spread_pct: float = 0.0  # bid-ask spread pct; from execution.json spread_threshold_pct check
    asset_class: str = "equity"  # "equity" | "options" | "futures"
    engine: str = "swing"  # "intraday" | "swing" — selects the per-engine budget in G7
    # Sector this symbol belongs to (from orchestrator._SYMBOL_TO_SECTOR, same
    # map sector_declining already uses); "" = unmapped, sector cap check is a no-op.
    sector: str = ""
    # RegimeFilter.max_attempts_per_phase bookkeeping (spec §8.2) — populated by
    # the orchestrator from strategy_regime_attempts. 0 max = not configured/off.
    regime_phase_attempts: int = 0
    max_regime_phase_attempts: int = 0
    # Per-strategy risk-% override (USSSpec.risk_pct) — percent of live equity to
    # risk on this trade, overriding operator.json's default_risk_pct. Bounded by
    # max_risk_per_trade_pct_ceiling. See effective_risk_cap().
    risk_pct: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CRMContext:
    """Runtime state injected into the CRM pipeline."""

    total_capital: float
    max_risk_per_trade: float
    max_daily_loss: float
    max_open_positions: int
    max_new_trades_per_day: int
    min_risk_reward_ratio: float
    max_stop_loss_pct: float

    daily_loss_so_far: float = 0.0
    current_equity: float = 0.0
    # Percentage-of-live-equity risk sizing (operator.json capital.*). default_risk_pct
    # applies to any strategy that sets no risk_pct of its own; the ceiling bounds both.
    # 0.0 on either means NOT CONFIGURED (no clamp / fall back to the rupee cap) — never
    # "risk zero", same convention as max_sector_exposure_pct.
    default_risk_pct: float = 0.0
    max_risk_per_trade_pct_ceiling: float = 0.0
    open_position_count: int = 0
    new_trades_today: int = 0
    consecutive_losses: int = 0

    # Phase 2 — independent risk counters per paper/live mode (a simulated paper
    # loss must never trip a real-money circuit breaker, and vice versa), keyed
    # "paper"/"live". G1/G2/G7 look up ctx.mode_*.get(mode, ctx.<legacy field>) —
    # missing key ⇒ falls back to the single field above unchanged, so any caller
    # not yet mode-aware (existing tests, backtest) is unaffected.
    mode_daily_loss_so_far: dict[str, float] = field(default_factory=dict)
    mode_open_position_count: dict[str, int] = field(default_factory=dict)
    mode_new_trades_today: dict[str, int] = field(default_factory=dict)
    mode_consecutive_losses: dict[str, int] = field(default_factory=dict)
    # Open (stop-to-fill) risk per mode, so paper positions never consume a live
    # trade's portfolio-risk budget (G7 open-risk cap).
    mode_open_risk_rupees: dict[str, float] = field(default_factory=dict)

    # Per-engine daily budgets (operator.json hard_limits.intraday / .swing).
    # 0 == not configured → the per-engine check is skipped (global backstop applies).
    max_intraday_trades_per_day: int = 0
    max_intraday_per_strategy: int = 0
    intraday_per_strategy_overrides: dict[str, int] = field(default_factory=dict)
    max_swing_trades_per_day: int = 0
    max_swing_per_strategy: int = 0
    swing_per_strategy_overrides: dict[str, int] = field(default_factory=dict)
    # operator.json hard_limits.{swing,intraday}.max_open_positions — concurrently
    # open positions PER ENGINE, independent of each other and of the shared global
    # max_open_positions. Without this, a swing book that fills its own budget
    # (positions held over many days) silently starves intraday of its entire
    # position budget too, since both engines shared one count (observed
    # 2026-08-10: 14 open swing positions blocked a same-day intraday entry that
    # had zero open positions of its own). 0 == not configured → falls back to the
    # shared global cap below for that engine.
    max_swing_open_positions: int = 0
    max_intraday_open_positions: int = 0
    # options_settings.json options_trading.position_limits.max_open_options_positions
    # -- found live 2026-09-10: seller_crm_adapter.py leaves signal.engine at its
    # default ("swing"), documented as a deliberate phase-1 stopgap since G7 had
    # no distinct options bucket -- but once swing's own small daily cap filled
    # (2/2), it started blocking every options candidate too, even with zero
    # options positions open. 0 == not configured -> falls back to the shared
    # global cap below, same convention as the other engines.
    max_options_open_positions: int = 0
    # options_settings.json position_limits.max_daily_loss_rupees -- G8's daily
    # budget check (max_daily_loss/daily_loss_so_far below) is a single
    # account-wide pot shared by every engine, sized for small equity paper
    # trades (operator.json capital.max_daily_loss_rupees=4000). Found live
    # 2026-09-10: that structurally can never afford even 1 real index-options
    # credit spread (max_loss commonly 15,000-20,000+), on ANY day, regardless
    # of what else traded. 0 == not configured -> falls back to the shared
    # global budget below, same convention as every other per-engine cap here.
    max_options_daily_loss: float = 0.0
    # config/capital.json options.max_structure_loss_per_strategy, in rupees at current
    # equity (src/risk/budget.py) -- the per-trade risk cap G8 uses for an options signal
    # INSTEAD of effective_risk_cap/operator.json's flat equity figure (2026-10-06: that
    # flat ~Rs 2,000 cap meant G8 silently shrank every real multi-lot options structure
    # to 1 lot, since a real spread's per-lot risk is usually 1,700-19,000). 0 == not
    # configured -> G8 falls back to effective_risk_cap, same convention as elsewhere.
    max_options_structure_loss_per_trade: float = 0.0
    # config/capital.json swing/intraday.max_loss_per_trade, in rupees at current equity
    # (src/risk/budget.py). G8 uses these for a swing/intraday signal INSTEAD of
    # effective_risk_cap when set (>0), same "new rupee field wins, 0 = fall back"
    # convention as the options field above (2026-10-07).
    max_swing_loss_per_trade: float = 0.0
    max_intraday_loss_per_trade: float = 0.0
    # config/capital.json swing/intraday.max_share_of_shared_risk, in rupees -- G7's
    # open-risk ceiling for that engine. For intraday this TAKES PRIORITY over the
    # older max_intraday_open_risk_pct x total_capital computation when set (>0);
    # swing had no equivalent check at all before this (2026-10-07).
    max_swing_shared_risk: float = 0.0
    max_intraday_shared_risk: float = 0.0
    # config/capital.json intraday.max_loss_per_day, in rupees -- intraday's OWN daily
    # stop, completely separate from swing (which has none) and from the legacy shared
    # max_daily_loss pot below. Checked per mode via mode_intraday_daily_loss_so_far
    # (realized + open MTM, INTRADAY/MIS positions only -- src.paper_trading.paper_engine
    # .daily_risk_counters(product="MIS")), refreshed every cycle same as the other
    # mode_* counters (2026-10-07).
    max_intraday_daily_loss: float = 0.0
    mode_intraday_daily_loss_so_far: dict[str, float] = field(default_factory=dict)
    # Today's REALIZED options loss (magnitude, 0 if net flat/positive), stamped every
    # cycle by SellerDesk._stamp_daily_loss -- before 2026-10-06 nothing ever wrote this,
    # so it stayed 0 all day and G8's "remaining budget" check (below) was effectively a
    # flat per-TRADE ceiling instead of a running per-DAY total.
    options_daily_loss_so_far: float = 0.0
    # Runtime per-engine / per-strategy counts, refreshed from the DB each cycle.
    intraday_trades_today: int = 0
    swing_trades_today: int = 0
    per_strategy_trades_today: dict[str, int] = field(default_factory=dict)
    swing_open_positions: int = 0
    intraday_open_positions: int = 0
    options_open_positions: int = 0
    # Phase 2 — same per-engine counts, keyed "paper:intraday"/"live:swing" etc.
    # Missing key ⇒ falls back to the unscoped field above (same pattern as
    # mode_daily_loss_so_far).
    mode_engine_open_positions: dict[str, int] = field(default_factory=dict)
    mode_engine_trades_today: dict[str, int] = field(default_factory=dict)

    master_switch_on: bool = True
    # Per-segment overrides (intraday/swing/options), keyed by segment_of(signal).
    # Empty dict (default) ⇒ no override ⇒ identical behavior to before these
    # existed. segment_master_switch is AND-ed with master_switch_on above.
    # segment_autonomy_mode falls back to signal.meta["autonomy_mode"] when the
    # segment isn't listed, so existing callers/tests are unaffected.
    segment_master_switch: dict[str, bool] = field(default_factory=dict)
    segment_autonomy_mode: dict[str, str] = field(default_factory=dict)
    segment_shadow_mode: dict[str, bool] = field(default_factory=dict)
    # Phase 2 — which mode (paper/live) each segment resolves to. Empty/missing
    # segment ⇒ paper (fail-safe default, matches config/global.json's seeded
    # `true`). Used by mode_of() below and by G1/G2/G7 to pick the matching
    # paper_*/live_* counter pair.
    segment_paper_mode: dict[str, bool] = field(default_factory=dict)
    # Per-strategy "paper" | "live" override from strategies_registry.json;
    # takes precedence over segment_paper_mode in mode_of(). Empty when
    # paper_mode_locked (the orchestrator does not populate it then).
    strategy_execution_mode: dict[str, str] = field(default_factory=dict)
    # Per-strategy live broker route (src.brokers.routing.BrokerRoute) for registry entries
    # whose execution_mode is "live". The options desk rejects a LIVE-resolved strategy whose
    # route is not usable (R1, src.brokers.routing.route_rejection).
    strategy_routes: dict[str, Any] = field(default_factory=dict)
    # ── Per-broker equity and risk (config/global.json system.per_broker_equity, default
    # false). Off: none of these fields is read and mode_of() keeps "paper"/"live". On: a
    # live signal's mode is "live:<broker>" and broker_view() swaps in that broker's own
    # equity, baseline (its first fetched equity), drawdown, capital.json rupee caps and
    # counters; G1 blocks the broker when its equity is unknown, below the minimum viable
    # equity, or its daily-loss breaker fired today (entries_blocked_brokers).
    per_broker_equity: bool = False
    legacy_live_broker: str = "kite"  # broker of a live signal without a registry route
    mode_current_equity: dict[str, float] = field(default_factory=dict)
    mode_total_capital: dict[str, float] = field(default_factory=dict)
    mode_drawdown_pct: dict[str, float] = field(default_factory=dict)
    mode_rupee_caps: dict[str, dict[str, float]] = field(default_factory=dict)
    entries_blocked_brokers: dict[str, str] = field(default_factory=dict)  # broker -> IST day
    min_viable_equity_rupees: float = 0.0  # capital.json account.min_viable_equity_rupees
    allowed_regimes: dict[str, list[str]] = field(default_factory=dict)
    # strategy -> regimes in which it may NOT open new positions (exits are never affected).
    # llm_swing_agent: config/global.json llm_swing.block_regimes.
    blocked_regimes: dict[str, list[str]] = field(default_factory=dict)
    max_consecutive_losses: int = 3

    session_open_hour: int = 9
    session_close_hour: int = 15
    # All of these come from config/market_hours.json (src/market/sessions.py).
    morning_open_block_minutes: int = field(default_factory=_sessions.morning_open_block_minutes)
    last_entry_by_str: str = field(default_factory=lambda: f"{_sessions.last_entry_by():%H:%M}")

    # VIX circuit breakers (risk.json circuit_breakers)
    vix_halt_threshold: float = 35.0
    vix_reduce_threshold: float = 25.0
    vix_spike_intraday_pct: float = 15.0
    spread_threshold_pct: float = 0.5  # risk.json circuit_breakers.spread_threshold_pct
    # risk.json volatility_adjustment — size multipliers per VIX band. 0 → BLOCK new
    # trades (fail-safe). vix_above_25 reduces size; vix_above_35 halts (==0 default).
    vix_reduce_multiplier: float = 0.5
    vix_halt_multiplier: float = 0.0

    # Drawdown staircase (risk.json drawdown_staircase)
    drawdown_pct: float = 0.0  # populated at runtime from P&L tracking
    tier_1_pct: float = 3.0
    tier_1_size_mult: float = 0.5
    tier_1_min_quality: float = 75.0
    tier_2_pct: float = 5.0
    tier_2_size_mult: float = 0.25
    tier_2_regimes: list[str] = field(default_factory=lambda: ["STRONG_BULL"])
    tier_3_pct: float = 8.0
    tier_3_action: str = "HALT_ALL"
    tier_4_pct: float = 10.0
    tier_4_action: str = "CAPITAL_PRESERVATION"
    reset_on: str = "calendar_month_start"

    # Operator capital limits (operator.json capital + hard_limits)
    cash_buffer_rupees: float = 0.0
    max_single_position_pct_of_capital: float = 20.0
    # operator.json capital.max_total_exposure_rupees — portfolio-wide deployed-capital
    # ceiling. 0 disables the cap (fail-safe: it can only BLOCK new exposure, never size up).
    # open_exposure_rupees is injected each cycle from the live order monitor.
    max_total_exposure_rupees: float = 0.0
    open_exposure_rupees: float = 0.0
    # Portfolio-level open-risk cap (spec §6.3) — sum of (entry - current_sl) *
    # qty across all open positions, injected each cycle the same way
    # open_exposure_rupees is. 0 disables (fail-safe: can only BLOCK, never size up).
    max_portfolio_risk_pct: float = 0.0  # % of total_capital
    # Intraday-only open-risk cap (% of total_capital), per mode. Kept separate from
    # the portfolio cap so swing's multi-day book is never squeezed by it.
    max_intraday_open_risk_pct: float = 0.0
    open_risk_rupees: float = 0.0
    # Sector exposure cap (spec §6.4) — reuses orchestrator._SYMBOL_TO_SECTOR.
    max_sector_exposure_pct: float = 0.0  # % of total_capital
    sector_open_exposure_rupees: dict[str, float] = field(default_factory=dict)

    # Agent permissions (operator.json agent_permissions)
    can_trade_equity: bool = True
    can_trade_options: bool = True
    can_trade_futures: bool = False
    can_create_strategies: bool = True
    can_deploy_strategies_autonomously: bool = False
    strategy_deployment_requires_human: bool = True
    max_agent_created_strategies_live: int = 2

    # Phase 2.5 survival hardening — runtime-injected, defaults preserve current behavior
    correlation_block: bool = False  # HIGH/EXTREME correlation crisis — set by correlation_loop
    llm_bypass: bool = False  # LLM hurting alpha — set by sanity watchdog
    quality_raise: float = 0.0  # cost erosion override: 5=HIGH, 10=CRITICAL
    capital_critical: bool = False  # CRITICAL capital drift — halt new trades
    halt_trading_weeks: bool = False  # consecutive negative weeks >= halt_after_negative_weeks
    # Non-empty = the last counter/open-risk refresh failed, so the caps below would be
    # judged on 0 or stale figures. Blocks new entries until the next good refresh.
    risk_inputs_unverified: str = ""

    # Phase 4 — calibration-raised min confidence thresholds per strategy (set by calibration_loop)
    calibration_thresholds: dict[str, float] = field(default_factory=dict)

    # strategies_registry.json llm_gate_required / llm_originated (src/llm/gate_policy.py).
    # A strategy mapped True may NOT pass G9 without a genuine LLM verdict — paper or live,
    # even when the sanity watchdog has bypassed the gate. Unlisted names = not required.
    llm_gate_required: dict[str, bool] = field(default_factory=dict)

    # 2026-08-21 — strategies expectancy_loop paused for failing ~90%+ of the time
    # (win rate <=10% over the last 20 trades). Checked in G6 so ALL entry paths
    # (CRM scan loop, intraday, swing) honor it, not just whichever path happened
    # to build the signal — previously this only blocked in the CRM scan loop.
    paused_strategies: set[str] = field(default_factory=set)

    # G9 now BLOCKING — every trade needs a real LLM verdict at signal generation.
    # Floor below which a genuinely-evaluated signal is rejected. Calibration may
    # raise it per strategy. Kept < 0.50 so the neutral LLM-down fallback (which is
    # never marked `evaluated`) cannot be confused for a low score.
    g9_min_confidence: float = 0.35

    # Intraday MIS leverage multiple applied to available cash for affordability sizing
    # (risk.json position_sizing.mis_leverage). Risk cap still bounds loss per trade.
    mis_leverage: float = 5.0
    # Max intraday order size as % of the stock's 20-day average daily volume
    # (risk.json position_sizing.max_adv_participation_pct). 0 = no cap.
    max_adv_participation_pct: float = 0.0

    # G6 backtest-quality override (risk.json backtest_override) — when a strategy's
    # periodic off-market backtest failed, an individual live signal may still trade
    # if its own setup quality clears this (stricter than the normal 65/70 registry
    # bar) elevated bar, sized down rather than fully blocked.
    backtest_override_min_quality_score: float = 85.0
    backtest_override_size_mult: float = 0.5

    @classmethod
    def from_operator_json(cls, path: str | Path) -> CRMContext:
        """Build a CRMContext from operator.json. Read-once at startup."""
        data = json.loads(Path(path).read_text())
        cap = data["capital"]
        lim = data["hard_limits"]
        intra_lim = lim.get("intraday", {})
        swing_lim = lim.get("swing", {})
        perms = data.get("agent_permissions", {})
        return cls(
            total_capital=float(cap["total_capital_rupees"]),
            max_risk_per_trade=float(cap["max_risk_per_trade_rupees"]),
            default_risk_pct=float(cap.get("default_risk_pct", 0.0)),
            max_risk_per_trade_pct_ceiling=float(cap.get("max_risk_per_trade_pct_ceiling", 0.0)),
            max_daily_loss=float(cap["max_daily_loss_rupees"]),
            max_open_positions=int(lim["max_open_positions"]),
            max_new_trades_per_day=int(lim["max_new_trades_per_day"]),
            max_intraday_trades_per_day=int(intra_lim.get("max_new_trades_per_day", 0)),
            max_intraday_per_strategy=int(intra_lim.get("max_new_trades_per_day_per_strategy", 0)),
            intraday_per_strategy_overrides={
                str(k): int(v) for k, v in intra_lim.get("per_strategy_overrides", {}).items()
            },
            max_swing_trades_per_day=int(swing_lim.get("max_new_trades_per_day", 0)),
            max_swing_per_strategy=int(swing_lim.get("max_new_trades_per_day_per_strategy", 0)),
            swing_per_strategy_overrides={
                str(k): int(v) for k, v in swing_lim.get("per_strategy_overrides", {}).items()
            },
            max_swing_open_positions=int(swing_lim.get("max_open_positions", 0)),
            max_intraday_open_positions=int(intra_lim.get("max_open_positions", 0)),
            min_risk_reward_ratio=float(lim["min_risk_reward_ratio"]),
            max_stop_loss_pct=float(lim["max_stop_loss_pct"]) / 100.0,
            cash_buffer_rupees=float(cap.get("cash_buffer_rupees", 0)),
            max_total_exposure_rupees=float(cap.get("max_total_exposure_rupees", 0)),
            max_single_position_pct_of_capital=float(
                lim.get("max_single_position_pct_of_capital", 20)
            ),
            can_trade_equity=bool(perms.get("can_trade_equity", True)),
            can_trade_options=bool(perms.get("can_trade_options", True)),
            can_trade_futures=bool(perms.get("can_trade_futures", False)),
            can_create_strategies=bool(perms.get("can_create_strategies", True)),
            can_deploy_strategies_autonomously=bool(
                perms.get("can_deploy_strategies_autonomously", False)
            ),
            strategy_deployment_requires_human=bool(
                perms.get("strategy_deployment_requires_human", True)
            ),
            max_agent_created_strategies_live=int(
                perms.get("max_agent_created_strategies_live", 2)
            ),
        )

    @staticmethod
    def apply_risk_json(ctx: CRMContext, risk_cfg: dict[str, Any]) -> None:
        """Apply risk.json circuit breaker + drawdown staircase + hard-limit overrides.

        risk.json is the authoritative override layer: from_operator_json provides the
        base, and risk.json wins for any key it sets (loss-streak / daily-loss caps and
        the daily trade-count / open-position caps), so the config genuinely governs.
        """
        cb = risk_cfg.get("circuit_breakers", {})
        ctx.vix_halt_threshold = float(cb.get("vix_halt_threshold", 35))
        ctx.vix_reduce_threshold = float(cb.get("vix_reduce_threshold", 25))
        ctx.vix_spike_intraday_pct = float(cb.get("vix_spike_intraday_pct", 15))
        ctx.spread_threshold_pct = float(cb.get("spread_threshold_pct", 0.5))
        va = risk_cfg.get("volatility_adjustment", {})
        ctx.vix_reduce_multiplier = float(va.get("vix_above_25_multiplier", 0.5))
        ctx.vix_halt_multiplier = float(va.get("vix_above_35_multiplier", 0.0))
        if "consecutive_loss_count" in cb:
            ctx.max_consecutive_losses = int(cb["consecutive_loss_count"])
        if "daily_loss_limit_rupees" in cb:
            ctx.max_daily_loss = float(cb["daily_loss_limit_rupees"])

        hl = risk_cfg.get("hard_limits", {})
        if "max_new_trades_per_day" in hl:
            ctx.max_new_trades_per_day = int(hl["max_new_trades_per_day"])
        if "max_open_positions" in hl:
            ctx.max_open_positions = int(hl["max_open_positions"])

        ds = risk_cfg.get("drawdown_staircase", {})
        ctx.tier_1_pct = float(ds.get("tier_1_pct", 3))
        ctx.tier_1_size_mult = float(ds.get("tier_1_size_mult", 0.5))
        ctx.tier_1_min_quality = float(ds.get("tier_1_min_quality", 75))
        ctx.tier_2_pct = float(ds.get("tier_2_pct", 5))
        ctx.tier_2_size_mult = float(ds.get("tier_2_size_mult", 0.25))
        ctx.tier_2_regimes = list(ds.get("tier_2_regimes", ["STRONG_BULL"]))
        ctx.tier_3_pct = float(ds.get("tier_3_pct", 8))
        ctx.tier_3_action = str(ds.get("tier_3_action", "HALT_ALL"))
        ctx.tier_4_pct = float(ds.get("tier_4_pct", 10))
        ctx.tier_4_action = str(ds.get("tier_4_action", "CAPITAL_PRESERVATION"))
        ctx.reset_on = str(ds.get("reset_on", "calendar_month_start"))

        ps = risk_cfg.get("position_sizing", {})
        ctx.mis_leverage = float(ps.get("mis_leverage", 5.0))
        ctx.max_adv_participation_pct = float(ps.get("max_adv_participation_pct", 0.0))

        bto = risk_cfg.get("backtest_override", {})
        ctx.backtest_override_min_quality_score = float(bto.get("min_quality_score", 85))
        ctx.backtest_override_size_mult = float(bto.get("size_mult", 0.5))

        pr = risk_cfg.get("portfolio_risk", {})
        ctx.max_portfolio_risk_pct = float(pr.get("max_open_risk_pct", 0))
        ctx.max_intraday_open_risk_pct = float(pr.get("max_intraday_open_risk_pct", 0))
        ctx.max_sector_exposure_pct = float(pr.get("max_sector_exposure_pct", 0))


@dataclass
class CRMDecision:
    """Final CRM decision returned to the caller."""

    signal_id: str
    approved: bool
    final_quantity: int
    gate_results: list[GateResult]
    blocking_gate: str = ""
    blocking_reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(_IST))


def registry_name_of(signal: CRMSignal) -> str:
    """The strategies_registry.json name that per-strategy policy keys on. Usually
    strategy_name; a signal whose strategy_name is more specific (the options desk's
    "options_seller_desk::<type>") carries its registry name in meta["registry_name"]."""
    return str(signal.meta.get("registry_name") or signal.strategy_name)


_SAME_DAY_OPTION_STRATEGIES = frozenset(
    {"options_intraday_short_premium", "options_intraday_short_premium2", "options_expiry_day"}
)


def segment_of(signal: CRMSignal) -> str:
    """Classify a signal into a dashboard-configurable segment bucket.

    "options" for options signals, otherwise the signal's existing intraday/
    swing engine bucket (see engine_of) — no new fields needed on CRMSignal,
    asset_class and engine already carry this.
    """
    return "options" if signal.asset_class == "options" else signal.engine


def mode_of(signal: CRMSignal, ctx: CRMContext) -> str:
    """Resolve "paper" or "live" for this signal.

    A per-strategy execution_mode override wins; otherwise the segment's mode.
    Defaults to "paper" (fail-safe) when the segment isn't configured, same
    default direction as config/global.json's seeded segments.*.paper_mode=true.
    """
    override = getattr(ctx, "strategy_execution_mode", {}).get(signal.strategy_name)
    if override in ("paper", "live"):
        mode = str(override)
    else:
        is_paper = getattr(ctx, "segment_paper_mode", {}).get(segment_of(signal), True)
        mode = "paper" if is_paper else "live"
    if mode == "paper" or not getattr(ctx, "per_broker_equity", False):
        return mode
    # Per-broker risk: the broker the orchestrator routes this live signal to (R1). A route
    # without a usable broker yields "live:" -- no equity is ever keyed there, so G1 blocks.
    routes = getattr(ctx, "strategy_routes", {}) or {}
    route = routes.get(registry_name_of(signal)) or routes.get(signal.strategy_name)
    if route is not None:
        return f"live:{getattr(route, 'broker', None) or ''}"
    return f"live:{getattr(ctx, 'legacy_live_broker', 'kite')}"


# CRMContext mode_* dicts a per-broker view re-keys: an absent "live:<broker>" (or
# "live:<broker>:<engine>") entry falls back to the legacy combined "live" bucket.
_MODE_DICTS = (
    "mode_daily_loss_so_far",
    "mode_open_position_count",
    "mode_new_trades_today",
    "mode_consecutive_losses",
    "mode_open_risk_rupees",
    "mode_intraday_daily_loss_so_far",
    "mode_engine_open_positions",
    "mode_engine_trades_today",
)
_ENGINES = ("intraday", "swing", "options")


def per_key_rupee_caps(budget: Any, equity: float) -> dict[str, float]:
    """The capital.json rupee caps CRMContext carries as scalars, computed from ONE broker's
    equity with the same percentages (src/risk/budget.py RiskBudget)."""
    return {
        "max_intraday_loss_per_trade": budget.intraday.max_loss_per_trade_rupees(equity),
        "max_intraday_shared_risk": budget.intraday.max_share_of_shared_risk_rupees(equity),
        "max_intraday_daily_loss": budget.intraday.max_loss_per_day_rupees(equity),
        "max_swing_loss_per_trade": budget.swing.max_loss_per_trade_rupees(equity),
        "max_swing_shared_risk": budget.swing.max_share_of_shared_risk_rupees(equity),
        "max_options_daily_loss": budget.options.max_loss_per_day_all_strategies_rupees(equity),
        "max_options_structure_loss_per_trade": (
            budget.options.max_structure_loss_per_strategy_rupees(equity)
        ),
    }


def broker_view(ctx: CRMContext, mode: str) -> CRMContext:
    """`ctx` as seen by one live broker's signal. Identity (the same object) unless
    per_broker_equity is on and `mode` is "live:<broker>"; then a shallow copy whose
    equity, total capital (the broker's own baseline), drawdown and rupee caps are that
    broker's, and whose mode_* dicts fall back to the combined "live" entry when the
    broker has none. Never mutates `ctx`."""
    if not (getattr(ctx, "per_broker_equity", False) and mode.startswith("live:")):
        return ctx
    view = copy.copy(ctx)
    for name in _MODE_DICTS:
        src = getattr(ctx, name)
        d = dict(src)
        if mode not in d and "live" in src:
            d[mode] = src["live"]
        for eng in _ENGINES:
            if f"{mode}:{eng}" not in d and f"live:{eng}" in src:
                d[f"{mode}:{eng}"] = src[f"live:{eng}"]
        setattr(view, name, d)
    equity = float(ctx.mode_current_equity.get(mode, 0.0) or 0.0)
    view.current_equity = equity
    baseline = float(ctx.mode_total_capital.get(mode, 0.0) or 0.0)
    view.total_capital = baseline
    view.drawdown_pct = ctx.mode_drawdown_pct.get(
        mode, max(0.0, (baseline - equity) / baseline * 100.0) if baseline > 0 else 0.0
    )
    for cap_name, value in ctx.mode_rupee_caps.get(mode, {}).items():
        setattr(view, cap_name, float(value))
    return view


def _broker_entry_block(mode: str, ctx: CRMContext) -> str | None:
    """G1 reason a per-broker view must take no new entry, else None."""
    broker = mode[len("live:") :]
    today = datetime.now(_IST).date().isoformat()
    if ctx.entries_blocked_brokers.get(broker) == today:
        return f"New entries blocked for broker {broker or '?'} today (daily-loss breaker)"
    equity = ctx.mode_current_equity.get(mode)
    if equity is None or equity <= 0 or ctx.mode_total_capital.get(mode, 0.0) <= 0:
        return f"Broker equity unavailable for {broker or '<no broker>'}: no new entries"
    if ctx.min_viable_equity_rupees > 0 and equity < ctx.min_viable_equity_rupees:
        return (
            f"Broker {broker} equity {equity:.0f} below the minimum viable equity "
            f"{ctx.min_viable_equity_rupees:.0f} (capital.json account.min_viable_equity_rupees)"
        )
    return None


def is_shadow(signal: CRMSignal, ctx: CRMContext) -> bool:
    """True when this signal's segment is in shadow_mode.

    Not a gate: shadow signals still PASS the full G0-G10 pipeline and count
    as approved in stats — the caller decides, after approval, to log the
    decision instead of placing a real or paper order.
    """
    return bool(getattr(ctx, "segment_shadow_mode", {}).get(segment_of(signal), False))


def effective_risk_cap(risk_pct: float | None, ctx: CRMContext) -> float:
    """Rupees to risk on one trade, as a percentage of LIVE equity.

    Percentage-first so risk scales with the account instead of a flat rupee figure
    an operator has to remember to raise. Reads ctx.current_equity (refreshed from
    broker margins every cycle) rather than ctx.total_capital, which is deliberately
    the static operator baseline that anchors G1's absolute drawdown floor.

    Falls back to the flat ctx.max_risk_per_trade when no percentage is configured
    anywhere, so strategies without risk_pct keep their existing behavior exactly.

    Both percentage knobs treat 0.0 as "not configured", never as "risk nothing".
    """
    pct = risk_pct if (risk_pct is not None and risk_pct > 0) else ctx.default_risk_pct
    if pct <= 0 or ctx.current_equity <= 0:
        return ctx.max_risk_per_trade
    if ctx.max_risk_per_trade_pct_ceiling > 0:
        pct = min(pct, ctx.max_risk_per_trade_pct_ceiling)
    return ctx.current_equity * pct / 100


def intraday_risk_cap(ctx: CRMContext) -> float:
    """Rupees to risk on one intraday trade: config/capital.json
    intraday.max_loss_per_trade (ctx.max_intraday_loss_per_trade) when set, else the
    legacy effective_risk_cap. Shared by G8 and the intraday sizer so they cannot
    disagree -- they did 2026-10-07..09, and G7's open-risk check (which runs before G8
    trims size) blocked correctly-capped trades on the oversized intended risk."""
    if ctx.max_intraday_loss_per_trade > 0:
        return ctx.max_intraday_loss_per_trade
    return effective_risk_cap(None, ctx)


def swing_risk_cap(risk_pct: float | None, ctx: CRMContext) -> float:
    """Rupees to risk on one swing trade: config/capital.json swing.max_loss_per_trade
    (ctx.max_swing_loss_per_trade) when set, else the legacy effective_risk_cap. Shared
    by G8 and the swing sizer -- the sizer used the legacy cap 2026-10-07..09, and since
    G8 only sizes DOWN, swing trades ran at about half the configured risk."""
    if ctx.max_swing_loss_per_trade > 0:
        return ctx.max_swing_loss_per_trade
    return effective_risk_cap(risk_pct, ctx)


class CentralRiskManager:
    """
    10-gate CRM pipeline.

    evaluate(signal, context) → CRMDecision.
    All gates run sequentially; first BLOCK halts the pipeline.
    G9 is BLOCKING (REJECT / sub-floor confidence) but fails open without a verdict.
    """

    # Optional callback(signal, ctx, decision) run after every evaluation -- the orchestrator
    # points it at the `signals` decision journal. Must never affect the decision.
    decision_sink: Any = None

    def precheck_without_llm(self, signal: CRMSignal, ctx: CRMContext) -> CRMDecision:
        """Every gate EXCEPT G9, with no decision-sink write: lets a caller learn that a
        deterministic gate (caps, budget, time, regime...) will refuse BEFORE spending an
        LLM call on the signal. Found 2026-10-06: the options desk asked the LLM the same
        question every 5 min for a spread that G8's daily budget could never allow."""
        signal.meta["_llm_precheck"] = True
        try:
            return self._evaluate_gates(signal, ctx)
        finally:
            signal.meta.pop("_llm_precheck", None)

    def evaluate(self, signal: CRMSignal, ctx: CRMContext) -> CRMDecision:
        decision = self._evaluate_gates(signal, ctx)
        sink = self.decision_sink
        if sink is not None:
            try:
                sink(signal, ctx, decision)
            except Exception as exc:  # noqa: BLE001
                _log.error("crm_decision_sink_failed signal=%s err=%s", signal.signal_id, exc)
        return decision

    def _evaluate_gates(self, signal: CRMSignal, ctx: CRMContext) -> CRMDecision:
        _log.debug(
            "crm_evaluate_received signal=%s strategy=%s symbol=%s qty=%d entry=%.2f "
            "sl_pct=%.4f target_pct=%.4f engine=%s asset_class=%s mode=%s",
            signal.signal_id,
            signal.strategy_name,
            signal.symbol,
            signal.intended_quantity,
            signal.entry_price,
            signal.stop_loss_pct,
            signal.target_pct,
            signal.engine,
            signal.asset_class,
            mode_of(signal, ctx),
        )
        # Flag off (or paper): the same object. On: the routed broker's own risk context.
        ctx = broker_view(ctx, mode_of(signal, ctx))
        gates = [
            self._g0_master_switch,
            self._g1_hard_caps,
            self._g2_circuit_breakers,
            self._g3_time_of_day,
            self._g4_regime,
            self._g5_news,
            self._g6_strategy_health,
            self._g7_portfolio_margin,
            self._g8_sl_sizing,
            self._g9_llm_confirmation,
            self._g10_autonomy_mode,
        ]

        results: list[GateResult] = []
        final_quantity = signal.intended_quantity

        for gate_fn in gates:
            result = gate_fn(signal, ctx, final_quantity)
            results.append(result)

            if result.decision == GateDecision.MODIFY and result.modified_quantity is not None:
                final_quantity = result.modified_quantity

            if result.decision == GateDecision.BLOCK:
                _log.debug(
                    "crm_evaluate_found signal=%s outcome=BLOCK gate=%s reason=%s",
                    signal.signal_id,
                    result.gate_id,
                    result.reason,
                )
                return CRMDecision(
                    signal_id=signal.signal_id,
                    approved=False,
                    final_quantity=0,
                    gate_results=results,
                    blocking_gate=result.gate_id,
                    blocking_reason=result.reason,
                )

        _log.debug(
            "crm_evaluate_found signal=%s outcome=APPROVED requested_qty=%d final_qty=%d "
            "gates_passed=%d",
            signal.signal_id,
            signal.intended_quantity,
            final_quantity,
            len(results),
        )
        return CRMDecision(
            signal_id=signal.signal_id,
            approved=True,
            final_quantity=final_quantity,
            gate_results=results,
        )

    def _g0_master_switch(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        if not ctx.master_switch_on:
            return GateResult("G0", GateDecision.BLOCK, "Master switch is OFF")
        segment = segment_of(signal)
        if not ctx.segment_master_switch.get(segment, True):
            return GateResult(
                "G0", GateDecision.BLOCK, f"Master switch is OFF for segment={segment}"
            )
        if ctx.capital_critical:
            return GateResult(
                "G0", GateDecision.BLOCK, "Capital reconciliation CRITICAL — new trades halted"
            )
        # Agent permission checks (operator.json agent_permissions)
        if signal.asset_class == "futures" and not ctx.can_trade_futures:
            return GateResult(
                "G0", GateDecision.BLOCK, "Futures trading not permitted (can_trade_futures=False)"
            )
        if signal.asset_class == "options" and not ctx.can_trade_options:
            return GateResult(
                "G0", GateDecision.BLOCK, "Options trading not permitted (can_trade_options=False)"
            )
        if signal.asset_class == "equity" and not ctx.can_trade_equity:
            return GateResult(
                "G0", GateDecision.BLOCK, "Equity trading not permitted (can_trade_equity=False)"
            )
        return GateResult("G0", GateDecision.PASS)

    def _g1_hard_caps(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        if getattr(ctx, "per_broker_equity", False):
            _bmode = mode_of(signal, ctx)
            if _bmode.startswith("live:"):
                _block = _broker_entry_block(_bmode, ctx)
                if _block is not None:
                    return GateResult("G1", GateDecision.BLOCK, _block)
        # 25% ABSOLUTE FLOOR — hardcoded
        if ctx.total_capital > 0:
            equity_pct = ctx.current_equity / ctx.total_capital
            if equity_pct < _ABSOLUTE_FLOOR_PCT:
                return GateResult(
                    "G1",
                    GateDecision.BLOCK,
                    f"25% absolute floor breached: equity={ctx.current_equity:.0f} "
                    f"({equity_pct:.1%} of {ctx.total_capital:.0f})",
                )

        # Options gets its OWN daily-loss circuit breaker, checked against its own pot
        # (never the shared equity pot below, which operator.json sizes for intraday/
        # swing and has nothing to do with options P&L). A simple THRESHOLD, not a
        # per-trade sizing constraint: once today's realized options loss reaches the
        # ceiling, every new options entry is refused regardless of its own size --
        # existing positions are never touched here, each keeps its own stop.
        _seg = segment_of(signal)
        if _seg == "options":
            if (
                ctx.max_options_daily_loss > 0
                and ctx.options_daily_loss_so_far >= ctx.max_options_daily_loss
            ):
                return GateResult(
                    "G1",
                    GateDecision.BLOCK,
                    f"Daily options loss cap: {ctx.options_daily_loss_so_far:.0f} >= "
                    f"{ctx.max_options_daily_loss:.0f}",
                )
        elif _seg == "swing":
            # config/capital.json: swing has NO daily loss limit, by explicit operator
            # decision (2026-10-06) -- each trade lives by its own stop-loss and
            # target only. Never blocked here regardless of how the day is going.
            pass
        elif _seg == "intraday":
            # Intraday's OWN daily stop (config/capital.json intraday.max_loss_per_day),
            # per mode, completely independent of swing/options (2026-10-07) -- was
            # previously the shared max_daily_loss pot below, mixed with swing's P&L.
            _mode = mode_of(signal, ctx)
            _i_loss = ctx.mode_intraday_daily_loss_so_far.get(_mode, 0.0)
            if ctx.max_intraday_daily_loss > 0 and _i_loss >= ctx.max_intraday_daily_loss:
                return GateResult(
                    "G1",
                    GateDecision.BLOCK,
                    f"Daily intraday loss cap ({_mode}): {_i_loss:.0f} >= "
                    f"{ctx.max_intraday_daily_loss:.0f}",
                )
        else:
            # Legacy fallback for any signal not tagged options/swing/intraday.
            _mode = mode_of(signal, ctx)
            _daily_loss = ctx.mode_daily_loss_so_far.get(_mode, ctx.daily_loss_so_far)
            if _daily_loss >= ctx.max_daily_loss:
                return GateResult(
                    "G1",
                    GateDecision.BLOCK,
                    f"Daily loss cap ({_mode}): {_daily_loss:.0f} >= {ctx.max_daily_loss:.0f}",
                )

        if signal.stop_loss_pct > 0:
            rr = signal.target_pct / signal.stop_loss_pct
            # Compare at the 2dp precision the floor is quoted in: LLM levels sit on 0.01 ticks, so a
            # "1.8R" idea lands at e.g. 1.7958 and was blocked while logging "1.80 < 1.80". The block
            # message shows 3dp so a real miss is never mistaken for a rounding tie.
            if round(rr, 2) < round(ctx.min_risk_reward_ratio, 2):
                return GateResult(
                    "G1",
                    GateDecision.BLOCK,
                    f"R:R {rr:.3f} < minimum {ctx.min_risk_reward_ratio:.3f}",
                )

        # 1e-9 absorbs float noise only: a stop clamped to exactly the cap computes
        # |entry-sl|/entry as e.g. 0.08000000000000004 (far below one tick).
        if signal.stop_loss_pct > ctx.max_stop_loss_pct + 1e-9:
            return GateResult(
                "G1",
                GateDecision.BLOCK,
                f"SL {signal.stop_loss_pct:.2%} > max {ctx.max_stop_loss_pct:.2%}",
            )

        # max_single_position_pct_of_capital check (operator.json hard_limits)
        if signal.entry_price > 0 and ctx.max_single_position_pct_of_capital > 0:
            max_pos_value = ctx.total_capital * (ctx.max_single_position_pct_of_capital / 100.0)
            pos_value = signal.entry_price * qty
            if pos_value > max_pos_value:
                capped = max(0, int(max_pos_value / signal.entry_price))
                if capped == 0:
                    return GateResult(
                        "G1",
                        GateDecision.BLOCK,
                        f"Entry ₹{signal.entry_price:.0f} alone exceeds "
                        f"{ctx.max_single_position_pct_of_capital:.0f}% capital limit",
                    )
                return GateResult(
                    "G1",
                    GateDecision.MODIFY,
                    f"Position capped at {ctx.max_single_position_pct_of_capital:.0f}% of capital: "
                    f"{qty}→{capped}",
                    modified_quantity=capped,
                )

        return GateResult("G1", GateDecision.PASS)

    def _g2_circuit_breakers(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        _mode = mode_of(signal, ctx)
        _consec_losses = ctx.mode_consecutive_losses.get(_mode, ctx.consecutive_losses)
        result = _circuit_breaker.evaluate(_consec_losses, ctx.max_consecutive_losses)
        if result.tripped:
            return GateResult("G2", GateDecision.BLOCK, result.reason)

        if ctx.halt_trading_weeks:
            return GateResult(
                "G2",
                GateDecision.BLOCK,
                "Trading halted: consecutive negative weeks exceeded limit",
            )

        # Equity inputs only (equity open risk + paper_trades/trades counters); options
        # risk is refreshed separately, so a failure here must not skip the daily
        # options premium entries.
        if ctx.risk_inputs_unverified and signal.asset_class != "options":
            return GateResult(
                "G2",
                GateDecision.BLOCK,
                f"Risk inputs unverified: {ctx.risk_inputs_unverified}",
            )

        if ctx.correlation_block:
            return GateResult(
                "G2", GateDecision.BLOCK, "Correlation crisis (HIGH/EXTREME) — new entries blocked"
            )

        # VIX circuit breakers (risk.json circuit_breakers + volatility_adjustment)
        if signal.vix >= ctx.vix_halt_threshold:
            if ctx.vix_halt_multiplier <= 0:
                return GateResult(
                    "G2",
                    GateDecision.BLOCK,
                    f"VIX {signal.vix:.1f} >= halt threshold {ctx.vix_halt_threshold:.0f}",
                )
            return GateResult(
                "G2",
                GateDecision.MODIFY,
                f"VIX {signal.vix:.1f} >= halt threshold {ctx.vix_halt_threshold:.0f} "
                f"→ {ctx.vix_halt_multiplier:.0%} size",
                modified_quantity=max(1, int(qty * ctx.vix_halt_multiplier)),
            )
        if signal.vix >= ctx.vix_reduce_threshold:
            if ctx.vix_reduce_multiplier <= 0:
                return GateResult(
                    "G2",
                    GateDecision.BLOCK,
                    f"VIX {signal.vix:.1f} >= reduce threshold {ctx.vix_reduce_threshold:.0f} "
                    f"→ 0% size",
                )
            reduced = max(1, int(qty * ctx.vix_reduce_multiplier))
            return GateResult(
                "G2",
                GateDecision.MODIFY,
                f"VIX {signal.vix:.1f} >= reduce threshold {ctx.vix_reduce_threshold:.0f} "
                f"→ {ctx.vix_reduce_multiplier:.0%} size",
                modified_quantity=reduced,
            )

        # Spread check (risk.json circuit_breakers.spread_threshold_pct)
        if signal.spread_pct > ctx.spread_threshold_pct:
            return GateResult(
                "G2",
                GateDecision.BLOCK,
                f"Spread {signal.spread_pct:.2%} > threshold {ctx.spread_threshold_pct:.2%}",
            )

        # Drawdown staircase (risk.json drawdown_staircase)
        dd = ctx.drawdown_pct
        if dd >= ctx.tier_4_pct:
            return GateResult(
                "G2",
                GateDecision.BLOCK,
                f"Drawdown {dd:.1f}% >= tier4 {ctx.tier_4_pct:.0f}% → {ctx.tier_4_action}",
            )
        if dd >= ctx.tier_3_pct:
            return GateResult(
                "G2",
                GateDecision.BLOCK,
                f"Drawdown {dd:.1f}% >= tier3 {ctx.tier_3_pct:.0f}% → {ctx.tier_3_action}",
            )
        if dd >= ctx.tier_2_pct:
            # tier2: only trade in allowed regimes at reduced size
            if signal.regime not in ctx.tier_2_regimes:
                return GateResult(
                    "G2",
                    GateDecision.BLOCK,
                    f"Drawdown {dd:.1f}% tier2: regime {signal.regime!r} not in {ctx.tier_2_regimes}",
                )
            reduced = max(1, int(qty * ctx.tier_2_size_mult))
            return GateResult(
                "G2",
                GateDecision.MODIFY,
                f"Drawdown tier2 {dd:.1f}% → {ctx.tier_2_size_mult:.0%} size",
                modified_quantity=reduced,
            )
        if dd >= ctx.tier_1_pct:
            reduced = max(1, int(qty * ctx.tier_1_size_mult))
            return GateResult(
                "G2",
                GateDecision.MODIFY,
                f"Drawdown tier1 {dd:.1f}% → {ctx.tier_1_size_mult:.0%} size",
                modified_quantity=reduced,
            )

        return GateResult("G2", GateDecision.PASS)

    def _g3_time_of_day(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        now = datetime.now(_IST)
        if segment_of(signal) == "swing":
            # Daily-candle decisions: time of day is not a risk signal for swing.
            allowed, reason = _TIME_FILTER.check_swing(now)
            if not allowed:
                return GateResult("G3", GateDecision.BLOCK, reason)
            return GateResult("G3", GateDecision.PASS)
        strategy_type: str = signal.meta.get("strategy_type", "SWING")
        llm_confidence: float = float(signal.meta.get("llm_confidence", 1.0))
        # Multi-day defined-risk options structures are held for days, so entering at 14:35
        # vs 14:25 changes nothing about their risk; the 14:30-15:15 LLM-confidence bar (an
        # intraday idea) only starved them. Hours and the exit-only pre-close still apply.
        # Same-day option strategies (forced exit) keep the bar.
        multi_day_option = (
            segment_of(signal) == "options"
            and signal.strategy_name not in _SAME_DAY_OPTION_STRATEGIES
        )
        allowed, reason = _TIME_FILTER.check(
            now, strategy_type, llm_confidence, confidence_bar=not multi_day_option
        )
        if not allowed:
            return GateResult("G3", GateDecision.BLOCK, reason)
        return GateResult("G3", GateDecision.PASS)

    def _g4_regime(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        if signal.regime == "UNKNOWN":
            return GateResult("G4", GateDecision.BLOCK, "Regime UNKNOWN — no new entries allowed")
        blocked = ctx.blocked_regimes.get(signal.strategy_name, [])
        if signal.regime in blocked:
            return GateResult(
                "G4",
                GateDecision.BLOCK,
                f"Regime {signal.regime!r} is blocked for {signal.strategy_name} "
                f"(blocked regimes {blocked}) -- no new entries, exits unaffected",
            )
        allowed = ctx.allowed_regimes.get(signal.strategy_name, [])
        if allowed and signal.regime not in allowed:
            return GateResult(
                "G4",
                GateDecision.BLOCK,
                f"Regime {signal.regime!r} not in allowed list {allowed}",
            )
        if signal.sector_declining:
            return GateResult(
                "G4", GateDecision.BLOCK, "Sector in DECLINING rotation — no new entries"
            )
        # RegimeFilter.max_attempts_per_phase (spec §8.2): block re-entry after N
        # consecutive stop-outs within the current in-regime phase. 0 max = off.
        if (
            signal.max_regime_phase_attempts > 0
            and signal.regime_phase_attempts >= signal.max_regime_phase_attempts
        ):
            return GateResult(
                "G4",
                GateDecision.BLOCK,
                f"Regime-phase attempts {signal.regime_phase_attempts} >= max "
                f"{signal.max_regime_phase_attempts}",
            )
        return GateResult("G4", GateDecision.PASS)

    def _g5_news(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        if signal.news_halt:
            return GateResult("G5", GateDecision.BLOCK, "News halt active")
        if signal.earnings_proximity_days <= 5:
            return GateResult(
                "G5",
                GateDecision.BLOCK,
                f"Earnings in {signal.earnings_proximity_days} day(s)",
            )

        # Confidence adjustments — applied in-place to meta so G9 sees updated value.
        conf: float = float(signal.meta.get("llm_confidence", 1.0))
        reasons: list[str] = []

        if signal.high_magnitude_news:
            conf = max(0.0, conf - 0.30)
            reasons.append("HIGH_MAG_NEWS -0.30")

        if signal.rs_new_high:
            conf = min(1.0, conf + 0.10)
            reasons.append("RS_NEW_HIGH +0.10")

        if signal.meta.get("news_priority_aligned"):
            conf = min(1.0, conf + 0.10)
            reasons.append("NEWS_PRIORITY +0.10")

        if reasons:
            signal.meta["llm_confidence"] = conf
            return GateResult(
                "G5", GateDecision.PASS, f"Confidence adjusted: {', '.join(reasons)} → {conf:.2f}"
            )

        return GateResult("G5", GateDecision.PASS)

    def _g6_strategy_health(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        health = signal.strategy_health.upper()
        if health in ("SUSPENDED", "UNKNOWN"):
            return GateResult(
                "G6",
                GateDecision.BLOCK,
                f"Strategy health={health!r} — no new entries",
            )

        if signal.strategy_name in ctx.paused_strategies:
            return GateResult(
                "G6",
                GateDecision.BLOCK,
                f"Strategy {signal.strategy_name!r} paused — win rate <=10% over last 20 trades",
            )

        # Backtest is informational only — a periodic off-market backtest failing
        # never blocks a live signal (it's a confidence signal, not a gate). A
        # valid signal still proceeds to G9 (LLM confirmation); size is reduced
        # as a caution instead of rejecting outright.
        if not signal.meta.get("backtest_passed", True):
            new_qty = max(1, int(qty * ctx.backtest_override_size_mult))
            return GateResult(
                "G6",
                GateDecision.MODIFY,
                f"Backtest failed (informational only) → size {qty}→{new_qty} "
                f"({ctx.backtest_override_size_mult:.0%}×)",
                modified_quantity=new_qty,
            )

        # Cost-erosion quality raise: RED(10) = HEALTHY only; YELLOW(5) = blocks CAUTIOUS/STRESSED
        if ctx.quality_raise >= 10.0 and health != "HEALTHY":
            return GateResult(
                "G6",
                GateDecision.BLOCK,
                f"Cost erosion CRITICAL: only HEALTHY strategies permitted (health={health!r})",
            )
        if ctx.quality_raise >= 5.0 and health in ("STRESSED", "CAUTIOUS"):
            return GateResult(
                "G6",
                GateDecision.BLOCK,
                f"Cost erosion HIGH: health={health!r} below raised bar",
            )
        _multipliers = {"CAUTIOUS": 0.75, "DEGRADED": 0.50, "STRESSED": 0.25}
        multiplier = _multipliers.get(health, 1.00)
        if multiplier < 1.0:
            new_qty = max(1, int(qty * multiplier))
            if new_qty < qty:
                return GateResult(
                    "G6",
                    GateDecision.MODIFY,
                    f"Strategy health={health!r} → size {qty}→{new_qty} ({multiplier:.0%}×)",
                    modified_quantity=new_qty,
                )
        return GateResult("G6", GateDecision.PASS)

    def _g7_portfolio_margin(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        # Per-engine open-position cap (decoupled intraday/swing budgets) — checked
        # BEFORE the shared global cap so one engine's held positions can never
        # starve the other's budget. A per-engine cap of 0 means "not configured",
        # in which case the shared global cap below is the backstop for that engine.
        #
        # segment_of() (not raw signal.engine) -- options signals leave
        # signal.engine at its unrelated CRMSignal default ("swing", see
        # CRMSignal's own field comment); asset_class="options" is the real
        # signal, and segment_of() already correctly resolves it (same
        # helper mode_of()/is_shadow() already use here). Found live
        # 2026-09-10: using raw signal.engine meant every options trade was
        # silently evaluated against SWING's own daily-trade/open-position
        # caps, and got blocked the moment swing (unrelated) filled its own
        # small 2/day cap.
        engine = segment_of(signal)
        _mode = mode_of(signal, ctx)
        # 2026-10-07: the hosted strategies (expiry-day butterfly, short-premium strategies)
        # each already guarantee their own concurrency (their own once-a-day / one-open
        # checks) -- that IS their own slot, same
        # reasoning as the per-index fix made the same day. Counting them against the
        # desk's shared max_options_open_positions cap (or the generic account-wide
        # fallback) meant the desk's own continuous trading (a new structure every few
        # minutes, all day) could always fill that cap first and permanently starve a
        # strategy that only gets one real attempt a week. Found live: desk hit 3/3 open
        # positions from its own structures alone, repeatedly blocking the condor before
        # it ever got a chance. No open-position-count check applies to them at all.
        _hosted = engine == "options" and signal.strategy_name in _HOSTED_OPTIONS_STRATEGIES
        if not _hosted:
            if engine == "intraday":
                eng_open_cap = ctx.max_intraday_open_positions
                eng_open_count = ctx.mode_engine_open_positions.get(
                    f"{_mode}:intraday", ctx.intraday_open_positions
                )
            elif engine == "options":
                eng_open_cap = ctx.max_options_open_positions
                eng_open_count = ctx.mode_engine_open_positions.get(
                    f"{_mode}:options", ctx.options_open_positions
                )
            else:
                eng_open_cap = ctx.max_swing_open_positions
                eng_open_count = ctx.mode_engine_open_positions.get(
                    f"{_mode}:swing", ctx.swing_open_positions
                )
            if eng_open_cap > 0:
                if eng_open_count >= eng_open_cap:
                    return GateResult(
                        "G7",
                        GateDecision.BLOCK,
                        f"{engine} ({_mode}) open positions {eng_open_count} >= cap {eng_open_cap}",
                    )
            else:
                _open_count = ctx.mode_open_position_count.get(_mode, ctx.open_position_count)
                if _open_count >= ctx.max_open_positions:
                    return GateResult(
                        "G7",
                        GateDecision.BLOCK,
                        f"Open positions ({_mode}) {_open_count} >= max {ctx.max_open_positions}",
                    )
        # Per-engine + per-strategy daily NEW-trade caps.
        # A cap of 0 means "not configured" → skip (the global backstop below applies).
        # options has no per-engine daily-trade cap of its own yet (no
        # max_options_trades_per_day exists) -- found live 2026-09-10: it was
        # silently borrowing swing's cap via the `else` branch below, and swing
        # hitting its own (unrelated) 2/day cap blocked every options candidate
        # too. Skipping straight to the global backstop (max_new_trades_per_day,
        # checked unconditionally further down) is more correct than sharing a
        # budget that has nothing to do with options risk.
        if engine == "intraday":
            eng_cap = ctx.max_intraday_trades_per_day
            eng_count = ctx.mode_engine_trades_today.get(
                f"{_mode}:intraday", ctx.intraday_trades_today
            )
            strat_default = ctx.max_intraday_per_strategy
            strat_overrides = ctx.intraday_per_strategy_overrides
        elif engine == "options":
            eng_cap = 0
            eng_count = 0
            strat_default = 0
            strat_overrides = {}
        else:
            eng_cap = ctx.max_swing_trades_per_day
            eng_count = ctx.mode_engine_trades_today.get(f"{_mode}:swing", ctx.swing_trades_today)
            strat_default = ctx.max_swing_per_strategy
            strat_overrides = ctx.swing_per_strategy_overrides
        if eng_cap > 0 and eng_count >= eng_cap:
            return GateResult(
                "G7",
                GateDecision.BLOCK,
                f"{engine} ({_mode}) trades today {eng_count} >= cap {eng_cap}",
            )
        strat_cap = int(strat_overrides.get(signal.strategy_name, strat_default))
        if strat_cap > 0:
            strat_count = int(ctx.per_strategy_trades_today.get(signal.strategy_name, 0))
            if strat_count >= strat_cap:
                return GateResult(
                    "G7",
                    GateDecision.BLOCK,
                    f"{signal.strategy_name} ({engine}) trades {strat_count} >= cap {strat_cap}",
                )
        _uncapped = engine == "options" and signal.strategy_name in _UNCAPPED_OPTIONS_STRATEGIES
        if _uncapped:
            return GateResult(
                "G7", GateDecision.PASS, "[uncapped] options_intraday_short_premium runs daily"
            )
        _new_trades = ctx.mode_new_trades_today.get(_mode, ctx.new_trades_today)
        if _new_trades >= ctx.max_new_trades_per_day:
            return GateResult(
                "G7",
                GateDecision.BLOCK,
                f"New trades today ({_mode}) {_new_trades} >= max {ctx.max_new_trades_per_day}",
            )
        # operator.json capital.max_total_exposure_rupees — portfolio deployed-capital cap.
        if ctx.max_total_exposure_rupees > 0 and signal.entry_price > 0:
            new_value = signal.entry_price * qty
            projected = ctx.open_exposure_rupees + new_value
            if projected > ctx.max_total_exposure_rupees:
                return GateResult(
                    "G7",
                    GateDecision.BLOCK,
                    f"Exposure ₹{projected:.0f} > cap ₹{ctx.max_total_exposure_rupees:.0f} "
                    f"(open ₹{ctx.open_exposure_rupees:.0f} + new ₹{new_value:.0f})",
                )

        # Portfolio-level open-risk cap (spec §6.3): sum of open positions' own
        # (entry - current_sl) risk, plus this trade's, vs a % of total capital.
        # signal.risk_rupees is the trade's TOTAL rupee risk (per-share risk x qty).
        if ctx.max_portfolio_risk_pct > 0 and signal.risk_rupees > 0:
            risk_cap = ctx.total_capital * ctx.max_portfolio_risk_pct / 100
            _open_risk = ctx.mode_open_risk_rupees.get(_mode, ctx.open_risk_rupees)
            projected_risk = _open_risk + signal.risk_rupees
            if projected_risk > risk_cap:
                return GateResult(
                    "G7",
                    GateDecision.BLOCK,
                    f"Portfolio open risk ({_mode}) ₹{projected_risk:.0f} > cap ₹{risk_cap:.0f} "
                    f"(open ₹{_open_risk:.0f} + new ₹{signal.risk_rupees:.0f})",
                )
        if engine == "intraday" and signal.risk_rupees > 0:
            # config/capital.json intraday.max_share_of_shared_risk (rupees at current
            # equity) takes priority when set; the older pct-of-total_capital figure
            # stays as a fallback for anyone not yet on the new config (2026-10-07).
            i_cap = (
                ctx.max_intraday_shared_risk
                if ctx.max_intraday_shared_risk > 0
                else ctx.total_capital * ctx.max_intraday_open_risk_pct / 100
            )
            if i_cap > 0:
                i_open = ctx.mode_open_risk_rupees.get(f"{_mode}:intraday", 0.0)
                if i_open + signal.risk_rupees > i_cap:
                    return GateResult(
                        "G7",
                        GateDecision.BLOCK,
                        f"Intraday open risk ({_mode}) ₹{i_open + signal.risk_rupees:.0f} > cap "
                        f"₹{i_cap:.0f} (open ₹{i_open:.0f} + new ₹{signal.risk_rupees:.0f})",
                    )
        if ctx.max_swing_shared_risk > 0 and engine == "swing" and signal.risk_rupees > 0:
            # New 2026-10-07: swing had NO open-risk ceiling at all before this --
            # config/capital.json swing.max_share_of_shared_risk.
            s_open = ctx.mode_open_risk_rupees.get(f"{_mode}:swing", 0.0)
            if s_open + signal.risk_rupees > ctx.max_swing_shared_risk:
                return GateResult(
                    "G7",
                    GateDecision.BLOCK,
                    f"Swing open risk ({_mode}) ₹{s_open + signal.risk_rupees:.0f} > cap "
                    f"₹{ctx.max_swing_shared_risk:.0f} (open ₹{s_open:.0f} + new "
                    f"₹{signal.risk_rupees:.0f})",
                )

        # Sector exposure cap (spec §6.4): this symbol's sector's deployed
        # capital, including this trade, vs a % of total capital.
        if ctx.max_sector_exposure_pct > 0 and signal.sector and signal.entry_price > 0:
            sector_cap = ctx.total_capital * ctx.max_sector_exposure_pct / 100
            new_value = signal.entry_price * qty
            sector_open = ctx.sector_open_exposure_rupees.get(signal.sector, 0.0)
            projected_sector = sector_open + new_value
            if projected_sector > sector_cap:
                return GateResult(
                    "G7",
                    GateDecision.BLOCK,
                    f"Sector {signal.sector!r} exposure ₹{projected_sector:.0f} > cap "
                    f"₹{sector_cap:.0f} (open ₹{sector_open:.0f} + new ₹{new_value:.0f})",
                )
        return GateResult("G7", GateDecision.PASS)

    def _g8_sl_sizing(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        if signal.stop_loss_pct <= 0:
            return GateResult("G8", GateDecision.BLOCK, "stop_loss_pct must be > 0")

        # config/capital.json per-segment per-trade rupee caps (src/risk/budget.py) win
        # over the legacy flat/equity-pct effective_risk_cap when set, same "new field
        # wins, 0 = fall back" convention for every segment (2026-10-07).
        _seg8 = segment_of(signal)
        if _seg8 == "options" and ctx.max_options_structure_loss_per_trade > 0:
            risk_cap = ctx.max_options_structure_loss_per_trade
        elif _seg8 == "swing" and ctx.max_swing_loss_per_trade > 0:
            risk_cap = swing_risk_cap(signal.risk_pct, ctx)
        elif _seg8 == "intraday" and ctx.max_intraday_loss_per_trade > 0:
            risk_cap = intraday_risk_cap(ctx)
        else:
            risk_cap = effective_risk_cap(signal.risk_pct, ctx)

        # Compute risk per share (= SL distance in rupees)
        if signal.entry_price > 0:
            sl_price = signal.entry_price * (1.0 - signal.stop_loss_pct)
            cap_qty = calculate_quantity(signal.entry_price, sl_price, risk_cap)
            risk_per_unit = signal.entry_price * signal.stop_loss_pct
        elif qty > 0 and signal.risk_rupees > 0:
            risk_per_unit = signal.risk_rupees / qty
            cap_qty = max(1, int(risk_cap / risk_per_unit))
        else:
            return GateResult("G8", GateDecision.BLOCK, "Cannot compute risk per unit")

        # BLOCK only if even 1 unit exceeds remaining daily budget -- the LEGACY
        # shared pot. Skipped entirely for options/swing/intraday (2026-10-06/07): a
        # daily-loss ceiling sized as a true circuit breaker (a fraction of a percent
        # of equity) sits far below a single real trade's own risk, so rationing
        # QUANTITY against it here would reject (or wrongly shrink) trades outright,
        # independent of today's actual P&L -- the exact bug a prior flat-rupee
        # options pot already hit once. G1's own per-segment threshold checks now own
        # "stop trading this segment for today" (swing has none, by design); per-trade
        # SIZE is governed separately by max_{options,swing,intraday}_loss_per_trade
        # / margin-Greeks-ROM sizing above, never by this shared legacy pot.
        if segment_of(signal) in ("options", "swing", "intraday"):
            remaining = float("inf")
        else:
            remaining = ctx.max_daily_loss - ctx.daily_loss_so_far
        if risk_per_unit > remaining:
            return GateResult(
                "G8",
                GateDecision.BLOCK,
                f"Daily budget: 1 unit costs ₹{risk_per_unit:.0f}, remaining ₹{remaining:.0f}",
            )

        # cash_buffer_rupees: reserve capital not available for trading (operator.json)
        deployable = ctx.total_capital - ctx.cash_buffer_rupees
        if signal.entry_price > 0 and deployable > 0:
            cash_cap_qty = max(0, int(deployable / signal.entry_price))
            cap_qty = min(cap_qty, cash_cap_qty)

        # Final quantity: capped by risk limit and daily budget, never sized up
        # Options carry no daily-budget rationing here (remaining = inf, see above):
        # int(inf) would raise OverflowError on every options signal.
        budget_qty = qty if remaining == float("inf") else int(remaining / risk_per_unit)
        final_qty = min(qty, cap_qty, budget_qty)
        if final_qty <= 0:
            return GateResult(
                "G8", GateDecision.BLOCK, "No capacity after cash_buffer_rupees reserve"
            )

        if final_qty < qty:
            return GateResult(
                "G8",
                GateDecision.MODIFY,
                f"Sized {qty}→{final_qty} (cap ₹{risk_cap:.0f})",
                modified_quantity=final_qty,
            )
        return GateResult("G8", GateDecision.PASS)

    def _g9_llm_confirmation(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        # BLOCKING gate. Per-strategy `llm_gate_required` (strategies_registry.json):
        # required -> no genuine verdict means NO trade (paper or live), and the
        # sanity-watchdog bypass does not apply. Not required -> the two fail-OPEN
        # escape hatches remain: (a) watchdog bypass, (b) no verdict produced.
        if signal.meta.get("_llm_precheck"):
            return GateResult("G9", GateDecision.PASS, "[precheck] LLM not consulted yet")
        required = ctx.llm_gate_required.get(registry_name_of(signal), False)
        if ctx.llm_bypass and not required:
            return GateResult(
                "G9", GateDecision.PASS, "[bypass] LLM gate disabled by sanity watchdog"
            )
        if not signal.meta.get("llm_evaluated", False):
            if required:
                return GateResult(
                    "G9",
                    GateDecision.BLOCK,
                    "LLM gate required but no verdict — "
                    f"{signal.meta.get('llm_reason') or 'no provider answered'}",
                )
            return GateResult("G9", GateDecision.PASS, "[no-verdict] LLM gate not required")
        if signal.meta.get("llm_flag", "") == "REJECT":
            return GateResult(
                "G9",
                GateDecision.BLOCK,
                f"LLM rejected by {signal.meta.get('llm_provider') or '?'}: "
                f"{signal.meta.get('llm_reason', 'no reason')}",
            )
        # Block when confidence is too low: the higher of the global floor and any
        # calibration-raised per-strategy threshold.
        floor = max(
            ctx.g9_min_confidence, ctx.calibration_thresholds.get(signal.strategy_name, 0.0)
        )
        llm_conf = float(signal.meta.get("llm_confidence", 1.0))
        if llm_conf < floor:
            return GateResult(
                "G9",
                GateDecision.BLOCK,
                f"LLM confidence {llm_conf:.2f} below floor {floor:.2f}",
            )
        who = signal.meta.get("llm_provider") or "?"
        ms = signal.meta.get("llm_latency_ms")
        return GateResult(
            "G9",
            GateDecision.PASS,
            f"{signal.meta.get('llm_flag') or 'OK'} conf={llm_conf:.2f} by {who}"
            + (f" in {int(ms)}ms" if ms else "")
            + (
                f": {str(signal.meta.get('llm_reason') or '')[:90]}"
                if signal.meta.get("llm_reason")
                else ""
            ),
        )

    def _g10_autonomy_mode(self, signal: CRMSignal, ctx: CRMContext, qty: int) -> GateResult:
        segment = segment_of(signal)
        mode = ctx.segment_autonomy_mode.get(segment) or signal.meta.get(
            "autonomy_mode", "autonomous"
        )
        if mode == "manual":
            return GateResult(
                "G10", GateDecision.BLOCK, "autonomy_mode=manual: requires human approval"
            )
        return GateResult("G10", GateDecision.PASS)
