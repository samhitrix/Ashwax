"""Seller-desk orchestration — the full decision + execution pipeline.

Connects every module built this session into one flow:

  chain_fetcher/volatility_engine (already looped, foundation sub-project)
    -> regime_classifier + event_calendar
    -> strategy_selector.candidates() (regime+IVR gate)
    -> structure_builder (real legs per candidate, from strike_selector)
    -> real EV computed from actual leg premiums (spec §4's own EV formula:
       AverageWin/AverageLoss tied directly to position_manager's own
       profit-target/stop-loss percentages — not invented separately)
    -> rom_tracker + position_sizer (margin + Greeks budget gates)
    -> options_leg_executor (protection-first placement)
    -> in-memory position tracking for the monitoring loops, mirrored to
       options_positions (migration 051) when a repo is injected

Position state lives primarily in memory (self.positions); when a repo is
injected (see __init__), every open/close is best-effort persisted to
options_positions so restore_positions() can rebuild self.positions after a
process restart — see run_cycle's insert_position call and
_close_position's close_position call. Persistence never blocks a live
trade: a DB failure here is logged and swallowed, not raised. Entry-time
Greeks are NOT round-tripped precisely (no per-position live Greeks
tracking). Realized PnL at close (options_positions.realized_pnl, added
2026-09-04) is computed from each leg's LTP captured immediately before its
market close order is placed (options_leg_executor.close_leg) -- an
APPROXIMATION of the real fill price, not a confirmed one (no fill-polling
exists for this close path, unlike the equity OrderMonitor). If any leg's
LTP fetch fails, realized_pnl is stored as NULL (incomplete) rather than a
partially-wrong number.

Margin is estimated via the standard defined-risk-spread convention
(width x lot_size - credit received) — not a real SPAN margin call, since
this build has no verified pre-trade margin API. Documented approximation,
not fabricated precision.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from src.notifications import EventKind, notify
from src.notifications.events import Segment
from src.options import seller_settings as _seller_settings
from src.options.adjustment_engine import estimate_adjustment_cost, evaluate_adjustment
from src.options.ev_calibration_gate import calibrated_ev_inputs, ev_calibration_mode
from src.options.expiry_guardian import evaluate as guardian_evaluate
from src.options.expiry_selector import dte as compute_dte
from src.options.expiry_selector import pre_entry_dte_check_from_expiry, select_calendar_expiries
from src.options.gap_risk_monitor import blocking_check as gap_blocking_check
from src.options.gap_risk_monitor import run_gap_stress_test
from src.options.greeks import DEFAULT_RISK_FREE_RATE, days_to_years
from src.options.greeks import greeks as bs_greeks
from src.options.greeks_budget import GreeksBudget, PortfolioGreeks, to_rupees
from src.options.greeks_monitor import (
    LegGreekInputs,
    PortfolioGreeksRs,
    check_delta_budget,
    check_gamma_budget,
    check_vega_budget,
    pnl_attribution_native,
)
from src.options.margin_monitor import evaluate_position_margin, new_entries_blocked
from src.options.options_autopsy import run_autopsy
from src.options.portfolio_loss_monitor import circuit_breaker_check
from src.options.position_manager import (
    ManagementAction,
    check_delta_breach_any_leg,
    single_sided_tested_delta,
)
from src.options.position_manager import evaluate_position as pm_evaluate_position
from src.options.position_sizer import size_position
from src.options.rom_tracker import rom_gate
from src.options.seller_crm_adapter import build_seller_crm_signal
from src.options.short_premium import SHORT_PREMIUM_TYPE, is_strangle_position
from src.options.strategy_selector import (
    StrategySelector,
    calibrated_pop,
    compute_ev,
    debit_vertical_pop,
    estimate_pop,
    sizing_multiplier,
)
from src.options.strike_selector import find_strike_by_delta, row_is_liquid
from src.options.structure_builder import (
    build_structure,
    build_two_expiry_structure,
    leg_from_chain_row,
)
from src.options.structure_builder import short_delta as target_short_delta
from src.options.structure_selector import OptionLeg
from src.risk.crm import CentralRiskManager, CRMContext, registry_name_of
from src.risk.gate_log import log_gate_decisions

if TYPE_CHECKING:
    from src.db.repositories.options import OptionsRepository
    from src.options.chain_fetcher import ChainFetcher
    from src.options.event_calendar import EventCalendar
    from src.options.nfo_chain import OptionChain
    from src.options.options_leg_executor import OptionsLegExecutor

_LOG = logging.getLogger("options.seller_desk")

# Directional side of each vertical. Every index in the universe (NIFTY/BANKNIFTY) moves
# together (~0.9), so two same-side spreads are one bet counted twice.
_DIRECTIONAL_SIDE = {
    "bull_put_spread": "bull",
    "bull_call_spread": "bull",
    "bear_call_spread": "bear",
    "bear_put_spread": "bear",
}

# Credit verticals whose width is fitted to the per-lot risk budget (see evaluate_and_size).
_WIDTH_CAPPED_TYPES = frozenset({"bull_put_spread", "bear_call_spread"})
_IST = ZoneInfo("Asia/Kolkata")

# Hypothesis B (config options_settings.json weekly_condor_simple): its own position
# family -- own one-per-underlying slot, own TP/SL, never rolled.
WEEKLY_CONDOR_TYPE = "weekly_condor_simple"
# Hypothesis F2 (config options_settings.json options_expiry_day).
EXPIRY_DAY_TYPE = "options_expiry_day"
_SINGLE_SIDED_TYPES = frozenset({"bull_put_spread", "bear_call_spread"})
# Hosted strategies that hold their own slot: the DB duplicate-reconciler must never
# delete their rows (a 4th NIFTY row would otherwise be hard-deleted as a "duplicate").
HOSTED_STRATEGY_TYPES: frozenset[str] = frozenset(
    {WEEKLY_CONDOR_TYPE, EXPIRY_DAY_TYPE, SHORT_PREMIUM_TYPE}
)
# Desk-hosted strategies that carry their OWN paper/live mode (registry execution_mode).
HOSTED_OWN_MODE_TYPES = frozenset({WEEKLY_CONDOR_TYPE, EXPIRY_DAY_TYPE, SHORT_PREMIUM_TYPE})
# The desk's OWN structures (bull_put_spread, iron_condor, ...) all trade under this one
# registry entry -- matches seller_crm_adapter.build_seller_crm_signal's own default.
DESK_OWN_STRATEGY_NAME = "options_seller_desk_v3"


def _registry_strategy_for(structure_type: str) -> str:
    """Map an OpenPosition.strategy_type (a STRUCTURE, e.g. "bull_put_spread",
    "iron_condor") to its REGISTRY strategy name -- the unit config/capital.json's
    per-strategy daily-loss kill operates on. Every desk-own structure type shares one
    registry entry; the hosted strategies (B, F) are already registry-named 1:1."""
    return structure_type if structure_type in HOSTED_OWN_MODE_TYPES else DESK_OWN_STRATEGY_NAME


_VERTICAL_OR_WINGED = frozenset(
    {
        "bull_put_spread",
        "bear_call_spread",
        "bull_call_spread",
        "bear_put_spread",
    }
)
# Four-leg wing structures: price can breach only ONE side at expiry, so max loss is
# the wider SIDE's own width, not the full span from the put wing to the call wing
# (2026-10-07 fix -- see _max_side_width).
_FOUR_LEG_WINGED = frozenset({"iron_condor", "iron_butterfly"})


@dataclass
class OpenPosition:
    position_id: str
    underlying: str
    strategy_type: str
    direction: str  # "credit" | "debit"
    protection_legs: list[OptionLeg]
    income_legs: list[OptionLeg]
    entry_premium: float
    entry_margin: float
    expiry: date
    entered_at: datetime
    adjustments_made: int = 0
    # Per-lot entry-time Greeks (see _per_lot_greeks). Stale by design -- not
    # refreshed after entry -- but real, unlike the zeros portfolio_greeks()
    # used to sum. Zero for rows whose entry_* columns were never populated.
    entry_greeks: PortfolioGreeks = field(default_factory=PortfolioGreeks)
    # Entry-time context for the post-close autopsy (migration 071); None on rows
    # opened before it existed -- the autopsy then skips the questions needing it.
    entry_regime: str | None = None
    entry_atm_iv: float | None = None
    entry_spot: float | None = None
    entry_short_delta: float | None = None
    was_breached: bool = False
    # High-water mark of unrealized P&L (rupees), persisted (migration 082) so a restart
    # does not forget it. Feeds position_manager.check_breakeven_lock (debit structures).
    peak_unrealized_pnl: float | None = None
    # Own paper/live mode. None = the desk's mode. B and options_expiry_day carry their
    # registry execution_mode here, so going live is ONE registry switch per strategy.
    is_paper: bool | None = None
    # options_intraday_short_premium only: legs already closed one at a time (per-leg SL), as the persisted
    # dicts (premium, exit_premium, lots, lot_size, direction ...), and the rules frozen at
    # entry (SL % per leg, exit time, LLM shadow verdict). Both ride in options_positions.legs.
    closed_legs: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def lots(self) -> int:
        legs = self.income_legs or self.protection_legs
        if not legs and self.closed_legs:
            return max(int(c.get("lots", 1)) for c in self.closed_legs) or 1
        return max((leg.lots for leg in legs), default=1) or 1

    @property
    def closed_pnl(self) -> float:
        """Realized rupees of the legs already closed (a short leg gains as price falls)."""
        total = 0.0
        for c in self.closed_legs:
            qty = int(c.get("lots", 1)) * int(c.get("lot_size", 1))
            sign = 1.0 if c.get("direction") == "BUY" else -1.0
            total += sign * (float(c["exit_premium"]) - float(c["premium"])) * qty
        return total


def _net_premium(protection_legs: list[OptionLeg], income_legs: list[OptionLeg]) -> float:
    total = 0.0
    for leg in protection_legs + income_legs:
        sign = -1.0 if leg.direction == "BUY" else 1.0
        total += sign * leg.premium * leg.quantity
    return total


def _structure_width(protection_legs: list[OptionLeg], income_legs: list[OptionLeg]) -> float:
    strikes = [leg.strike for leg in protection_legs + income_legs]
    return (max(strikes) - min(strikes)) if strikes else 0.0


def _max_side_width(protection_legs: list[OptionLeg], income_legs: list[OptionLeg]) -> float:
    """TRUE max-loss width for a 4-leg wing structure (iron condor / iron butterfly):
    the underlying can only finish beyond ONE side's strikes at expiry, so max loss is
    the WIDER of the two sides' own width, never the full span from the put wing to
    the call wing. Using the full span (the old _structure_width, still correct for a
    2-leg vertical) overstated max loss by roughly 2x for a symmetric structure -- it
    fed lot sizing, the ROM gate, paper margin commitment, and the persisted max_loss
    figure, all too conservative (found 2026-10-07)."""
    legs = protection_legs + income_legs
    calls = [leg.strike for leg in legs if leg.instrument_type == "CE"]
    puts = [leg.strike for leg in legs if leg.instrument_type == "PE"]
    call_width = (max(calls) - min(calls)) if len(calls) >= 2 else 0.0
    put_width = (max(puts) - min(puts)) if len(puts) >= 2 else 0.0
    return max(call_width, put_width)


def estimate_economics(
    strategy_type: str,
    protection_legs: list[OptionLeg],
    income_legs: list[OptionLeg],
    lot_size: int,
    lots: int,
) -> tuple[float, float, float, str]:
    """(max_loss, max_profit, net_premium, direction) from REAL leg premiums.

    width x lot_size - credit is the standard defined-risk vertical/IC/IB
    max-loss convention. Straddle/strangle max_profit is theoretically
    unbounded — bounded here at 1.5x debit paid as a documented EV-input
    simplification (no expected-move distribution model in this build).
    """
    net_premium = _net_premium(protection_legs, income_legs)
    direction = "credit" if net_premium > 0 else "debit"

    if strategy_type in _VERTICAL_OR_WINGED or strategy_type in _FOUR_LEG_WINGED:
        width = (
            _max_side_width(protection_legs, income_legs)
            if strategy_type in _FOUR_LEG_WINGED
            else _structure_width(protection_legs, income_legs)
        )
        notional_width = width * lot_size * lots
        if direction == "credit":
            return max(notional_width - net_premium, 0.0), net_premium, net_premium, direction
        debit = -net_premium
        return debit, max(notional_width - debit, 0.0), net_premium, direction

    debit = -net_premium
    return debit, debit * 1.5, net_premium, "debit"


def build_weekly_condor(
    chain: OptionChain, lots: int, *, short_delta: float, wing_points: float
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Hypothesis B's condor: short CE/PE at ~``short_delta`` (desk's liquidity-checked
    delta search), long wings exactly ``wing_points`` beyond each short. None when a
    short strike, a wing strike, or a wing's liquidity is missing -- never a
    substituted strike, so the traded structure is always the tested one."""
    short_c = find_strike_by_delta(chain, short_delta, "CE")
    short_p = find_strike_by_delta(chain, short_delta, "PE")
    if short_c is None or short_p is None or short_p.strike >= short_c.strike:
        return None
    by_strike = {row.strike: row for row in chain.strikes}
    long_c = by_strike.get(short_c.strike + wing_points)
    long_p = by_strike.get(short_p.strike - wing_points)
    if long_c is None or long_p is None:
        return None
    if not (row_is_liquid(long_c, "CE") and row_is_liquid(long_p, "PE")):
        return None
    legs = [
        leg_from_chain_row(long_p, "PE", "BUY", lots, chain.lot_size),
        leg_from_chain_row(long_c, "CE", "BUY", lots, chain.lot_size),
        leg_from_chain_row(short_p, "PE", "SELL", lots, chain.lot_size),
        leg_from_chain_row(short_c, "CE", "SELL", lots, chain.lot_size),
    ]
    if any(leg is None for leg in legs):
        return None
    full = cast("list[OptionLeg]", legs)
    return full[:2], full[2:]


def build_expiry_butterfly(
    chain: OptionChain, lots: int, *, wing_points: float
) -> tuple[list[OptionLeg], list[OptionLeg]] | None:
    """Hypothesis F2: short ATM CE + PE (strike nearest spot), long wings exactly
    ``wing_points`` away. All four legs must be liquid; never a substituted strike."""
    rows = [r for r in chain.strikes if r.call is not None and r.put is not None]
    if not rows:
        return None
    atm = min(rows, key=lambda r: abs(r.strike - chain.spot))
    by_strike = {r.strike: r for r in chain.strikes}
    up, dn = by_strike.get(atm.strike + wing_points), by_strike.get(atm.strike - wing_points)
    if up is None or dn is None:
        return None
    if not (
        row_is_liquid(atm, "CE")
        and row_is_liquid(atm, "PE")
        and row_is_liquid(up, "CE")
        and row_is_liquid(dn, "PE")
    ):
        return None
    legs = [
        leg_from_chain_row(dn, "PE", "BUY", lots, chain.lot_size),
        leg_from_chain_row(up, "CE", "BUY", lots, chain.lot_size),
        leg_from_chain_row(atm, "PE", "SELL", lots, chain.lot_size),
        leg_from_chain_row(atm, "CE", "SELL", lots, chain.lot_size),
    ]
    if any(leg is None for leg in legs):
        return None
    full = cast("list[OptionLeg]", legs)
    return full[:2], full[2:]


def model_credit_points(
    protection_legs: list[OptionLeg],
    income_legs: list[OptionLeg],
    spot: float,
    vix: float | None,
    now: datetime,
) -> float | None:
    """The research model's credit for these strikes (Black-Scholes, India VIX as flat IV,
    time to 15:30 in trading days) -- logged beside the REAL credit so the model's key
    assumption is checked on every live/paper entry (docs/hypotheses/F_*.md)."""
    if not vix or vix <= 0:
        return None
    from src.options.greeks import bs_price  # noqa: PLC0415

    mins = max((15 * 60 + 30) - (now.hour * 60 + now.minute), 1)
    t = mins / 375.0 / 252.0
    total = 0.0
    for leg in protection_legs + income_legs:
        p = bs_price(spot, leg.strike, t, DEFAULT_RISK_FREE_RATE, vix / 100.0, leg.instrument_type)
        total += p if leg.direction == "SELL" else -p
    return total


def first_trading_day_of_cycle(expiry: date) -> date:
    """First trading day after the previous weekly expiry (taken as expiry - 7 days).
    If that previous expiry was itself holiday-shifted, this can land one day late --
    the entry is then simply a day later, never an extra entry."""
    from datetime import timedelta  # noqa: PLC0415

    from src.market.calendar import is_trading_day  # noqa: PLC0415

    d = expiry - timedelta(days=7)
    for _ in range(10):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d
    return d


def _per_lot_greeks(
    chain: OptionChain,
    protection_legs: list[OptionLeg],
    income_legs: list[OptionLeg],
    lots: int,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> PortfolioGreeks:
    """Real gamma/theta/vega per lot, recomputed from the chain's stored IV
    per strike (Black-Scholes, same solver the rest of this build uses) —
    not left as an unimplemented zero."""
    t = days_to_years(chain.days_to_expiry)
    by_strike = {row.strike: row for row in chain.strikes}
    delta = gamma = theta = vega = 0.0
    if lots <= 0 or t <= 0.0:
        return PortfolioGreeks()
    for leg in protection_legs + income_legs:
        row = by_strike.get(leg.strike)
        if row is None:
            continue
        iv = row.call_iv if leg.instrument_type == "CE" else row.put_iv
        if iv is None:
            continue
        g = bs_greeks(chain.spot, leg.strike, t, risk_free_rate, iv, leg.instrument_type)
        sign = 1.0 if leg.direction == "BUY" else -1.0
        delta += sign * g["delta"] * leg.lot_size
        gamma += sign * g["gamma"] * leg.lot_size
        theta += sign * g["theta"] * leg.lot_size
        vega += sign * g["vega"] * leg.lot_size
    return PortfolioGreeks(
        net_delta=delta / lots, net_gamma=gamma / lots, net_theta=theta / lots, net_vega=vega / lots
    )


async def evaluate_and_size(
    *,
    strategy_type: str,
    chain: OptionChain,
    options_capital: float,
    risk_per_trade_pct: float,
    margin_safety_pct: float,
    available_margin: float,
    current_greeks: PortfolioGreeks,
    greeks_budget: GreeksBudget,
    dte_value: int,
    min_rom_pct: float,
    executor: OptionsLegExecutor,
    ivr: float | None = None,
    full_risk_per_trade_pct: float | None = None,
) -> tuple[list[OptionLeg], list[OptionLeg], int] | None:
    """Build a 1-lot trial, compute real EV/ROM/Greeks, then size it. Returns
    the final (protection_legs, income_legs, lots), or None on any gate fail.

    ``full_risk_per_trade_pct`` -- see position_sizer.size_position()'s own
    docstring: when ``risk_per_trade_pct`` has already been shrunk by a
    reduced-size multiplier, this lets sizing rescue the trade to exactly 1
    lot if it would fit the caller's full, unreduced budget instead of
    rejecting outright.

    ``ivr`` passes through to build_structure() for low_ivr_mode's tighter
    delta profile (off by default) -- see structure_builder._low_ivr_profile.
    """
    _LOG.debug(
        "evaluate_and_size_received strategy=%s underlying=%s dte=%d options_capital=%.0f "
        "risk_per_trade_pct=%.3f available_margin=%.0f min_rom_pct=%.2f",
        strategy_type,
        getattr(chain, "underlying", "?"),
        dte_value,
        options_capital,
        risk_per_trade_pct,
        available_margin,
        min_rom_pct,
    )
    built = build_structure(strategy_type, chain, 1, ivr=ivr)
    if built is None:
        _LOG.info(
            "seller_desk_no_trade_reason strategy=%s reason=structure_build_failed "
            "(no strikes within delta tolerance for this chain)",
            strategy_type,
        )
        return None
    trial_protection, trial_income = built
    lot_size = chain.lot_size

    max_loss_1lot, max_profit_1lot, net_premium_1lot, direction = estimate_economics(
        strategy_type, trial_protection, trial_income, lot_size, 1
    )
    # Width yields to the risk budget, not the other way round. The wing DELTA alone sets a
    # credit vertical's width -- ~1,800 points on BANKNIFTY, ~35-50k a lot against a 20k (2%)
    # limit -- so sizing rejected it every cycle and the desk never sold premium there
    # (2026-09-28). When one lot would not fit even the unreduced budget, pull the protective
    # wing in (the short strike, which earns the premium, keeps its delta).
    width_cap: float | None = None
    _full_budget = options_capital * (full_risk_per_trade_pct or risk_per_trade_pct) / 100.0
    if strategy_type in _WIDTH_CAPPED_TYPES and max_loss_1lot > _full_budget:
        narrowed = build_structure(strategy_type, chain, 1, ivr=ivr, max_loss_per_lot=_full_budget)
        if narrowed is not None:
            trial_protection, trial_income = narrowed
            width_cap = _full_budget
            _wide_loss = max_loss_1lot
            max_loss_1lot, max_profit_1lot, net_premium_1lot, direction = estimate_economics(
                strategy_type, trial_protection, trial_income, lot_size, 1
            )
            _LOG.info(
                "seller_desk_width_narrowed strategy=%s underlying=%s max_loss_1lot %.0f -> %.0f "
                "(budget %.0f) short=%.0f long=%.0f",
                strategy_type,
                getattr(chain, "underlying", "?"),
                _wide_loss,
                max_loss_1lot,
                _full_budget,
                trial_income[0].strike,
                trial_protection[0].strike,
            )
    if max_loss_1lot <= 0.0:
        _LOG.info(
            "seller_desk_no_trade_reason strategy=%s reason=degenerate_structure "
            "max_loss_1lot=%.2f",
            strategy_type,
            max_loss_1lot,
        )
        return None

    short_deltas = [abs(leg.delta) for leg in trial_income] or [
        abs(leg.delta) for leg in trial_protection
    ]
    # A directional debit vertical wins only beyond BREAKEVEN; 1-|short delta| is the
    # credit-spread event and overstated it ~0.75 vs ~0.43 (2026-09-28).
    debit_pop = debit_vertical_pop(
        strategy_type,
        trial_protection,
        trial_income,
        spot=chain.spot,
        iv=chain.atm_iv(),
        dte=dte_value,
    )
    pop = debit_pop if debit_pop is not None else calibrated_pop(strategy_type, short_deltas)
    avg_win, avg_loss, _calibrated_shadow = calibrated_ev_inputs(
        strategy_type, direction, net_premium_1lot, max_profit_1lot, max_loss_1lot
    )
    if _calibrated_shadow is not None and ev_calibration_mode() == "shadow":
        _heuristic_win = (
            (0.5 * net_premium_1lot) if direction == "credit" else (0.6 * max_profit_1lot)
        )
        _heuristic_loss = (1.5 * net_premium_1lot) if direction == "credit" else max_loss_1lot
        _LOG.info(
            "seller_desk_ev_shadow_compare strategy=%s heuristic_ev=%.2f calibrated_ev=%.2f "
            "heuristic_pass=%s calibrated_pass=%s",
            strategy_type,
            compute_ev(pop, _heuristic_win, _heuristic_loss),
            compute_ev(pop, _calibrated_shadow["avg_win"], _calibrated_shadow["avg_loss"]),
            compute_ev(pop, _heuristic_win, _heuristic_loss) > 0.0,
            compute_ev(pop, _calibrated_shadow["avg_win"], _calibrated_shadow["avg_loss"]) > 0.0,
        )
    ev = compute_ev(pop, avg_win, avg_loss)
    if ev <= 0.0:
        _LOG.info(
            "seller_desk_no_trade_reason strategy=%s reason=negative_ev ev=%.2f pop=%.3f "
            "avg_win=%.2f avg_loss=%.2f",
            strategy_type,
            ev,
            pop,
            avg_win,
            avg_loss,
        )
        return None

    if direction == "credit":
        passed_rom, rom_reason = rom_gate(
            net_premium_1lot, max_loss_1lot, dte_value, min_pct=min_rom_pct
        )
        if not passed_rom:
            _LOG.info(
                "seller_desk_no_trade_reason strategy=%s reason=rom_gate_failed detail=%s",
                strategy_type,
                rom_reason,
            )
            return None

    per_lot_greeks = to_rupees(
        _per_lot_greeks(chain, trial_protection, trial_income, 1), chain.spot
    )
    try:
        margin_1lot = await executor.get_order_margin_for_structure(
            trial_protection, trial_income, f"{strategy_type}-sizing-probe"
        )
    except Exception as exc:  # noqa: BLE001 — never trade sized on a fake number if this fails
        _LOG.error(
            "seller_desk_no_trade_reason strategy=%s reason=margin_fetch_failed detail=%s",
            strategy_type,
            exc,
        )
        return None
    if margin_1lot.total_margin <= 0.0 or margin_1lot.total_margin < max_loss_1lot:
        _LOG.error(
            "seller_desk_no_trade_reason strategy=%s reason=implausible_margin real=%.0f "
            "theoretical=%.0f -- refusing rather than trusting a parsing bug",
            strategy_type,
            margin_1lot.total_margin,
            max_loss_1lot,
        )
        return None
    sizing = size_position(
        options_capital,
        risk_per_trade_pct,
        max_loss_1lot,
        margin_per_lot=margin_1lot.total_margin,
        available_margin=available_margin,
        current_greeks=current_greeks,
        per_lot_greeks=per_lot_greeks,
        greeks_budget=greeks_budget,
        margin_safety_pct=margin_safety_pct,
        full_risk_per_trade_pct=full_risk_per_trade_pct,
    )
    if sizing.rejected or sizing.lots < 1:
        _LOG.info(
            "seller_desk_no_trade_reason strategy=%s reason=sizing_rejected detail=%s lots=%d "
            "max_loss_1lot=%.2f risk_budget=%.2f (capital=%.0f x risk_pct=%.3f)",
            strategy_type,
            sizing.reason,
            sizing.lots,
            max_loss_1lot,
            options_capital * risk_per_trade_pct / 100.0,
            options_capital,
            risk_per_trade_pct,
        )
        return None

    final = build_structure(strategy_type, chain, sizing.lots, ivr=ivr, max_loss_per_lot=width_cap)
    if final is None:
        _LOG.info(
            "seller_desk_no_trade_reason strategy=%s reason=final_structure_build_failed lots=%d",
            strategy_type,
            sizing.lots,
        )
        return None
    _LOG.info(
        "seller_desk_sized strategy=%s underlying=%s lots=%d max_loss_1lot=%.2f ev=%.2f pop=%.3f",
        strategy_type,
        getattr(chain, "underlying", "?"),
        sizing.lots,
        max_loss_1lot,
        ev,
        pop,
    )
    return final[0], final[1], sizing.lots


_TWO_EXPIRY_TYPES = frozenset({"calendar_spread", "diagonal_spread"})


def _opt_float(value: object) -> float | None:
    return None if value is None else float(value)  # type: ignore[arg-type]


def _per_lot_greeks_two_expiry(
    near_chain: OptionChain,
    far_chain: OptionChain,
    protection_legs: list[OptionLeg],
    income_legs: list[OptionLeg],
    lots: int,
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> PortfolioGreeks:
    """Same math as _per_lot_greeks, but protection_legs (far/BUY) price off
    far_chain and income_legs (near/SELL) price off near_chain — the two
    expiries have different IV surfaces, so a single by-strike lookup would
    silently zero out one side's Greeks contribution."""
    t_near = days_to_years(near_chain.days_to_expiry)
    t_far = days_to_years(far_chain.days_to_expiry)
    if lots <= 0 or t_near <= 0.0 or t_far <= 0.0:
        return PortfolioGreeks()
    delta = gamma = theta = vega = 0.0
    near_by_strike = {row.strike: row for row in near_chain.strikes}
    far_by_strike = {row.strike: row for row in far_chain.strikes}
    for legs, chain_by_strike, t, sign in (
        (income_legs, near_by_strike, t_near, -1.0),
        (protection_legs, far_by_strike, t_far, 1.0),
    ):
        for leg in legs:
            row = chain_by_strike.get(leg.strike)
            if row is None:
                continue
            iv = row.call_iv if leg.instrument_type == "CE" else row.put_iv
            if iv is None:
                continue
            spot = near_chain.spot  # same underlying, use near_chain's spot consistently
            g = bs_greeks(spot, leg.strike, t, risk_free_rate, iv, leg.instrument_type)
            delta += sign * g["delta"] * leg.lot_size
            gamma += sign * g["gamma"] * leg.lot_size
            theta += sign * g["theta"] * leg.lot_size
            vega += sign * g["vega"] * leg.lot_size
    return PortfolioGreeks(
        net_delta=delta / lots, net_gamma=gamma / lots, net_theta=theta / lots, net_vega=vega / lots
    )


async def evaluate_and_size_two_expiry(
    *,
    strategy_type: str,
    near_chain: OptionChain,
    far_chain: OptionChain,
    options_capital: float,
    risk_per_trade_pct: float,
    margin_safety_pct: float,
    available_margin: float,
    current_greeks: PortfolioGreeks,
    greeks_budget: GreeksBudget,
    directional_bias: float | None,
    executor: OptionsLegExecutor,
) -> tuple[list[OptionLeg], list[OptionLeg], int] | None:
    """calendar_spread / diagonal_spread counterpart to evaluate_and_size().
    Kept fully separate from the single-chain path — no shared mutable state,
    no signature change to the tested 8-strategy path. Both structures are
    net-debit by construction (far leg bought costs more than near leg sold),
    so estimate_economics()'s existing generic-debit branch (not
    _VERTICAL_OR_WINGED) applies unchanged: max_loss = debit paid, max_profit
    capped at 1.5x debit (same documented simplification already used for
    long_straddle/long_strangle — no expected-move distribution model here).
    No ROM gate: rom_gate() is credit-strategy-specific (same as the existing
    debit-strategy branch in evaluate_and_size, which also skips it)."""
    built = build_two_expiry_structure(strategy_type, near_chain, far_chain, 1, directional_bias)
    if built is None:
        return None
    trial_protection, trial_income = built

    max_loss_1lot, max_profit_1lot, net_premium_1lot, direction = estimate_economics(
        strategy_type, trial_protection, trial_income, near_chain.lot_size, 1
    )
    if max_loss_1lot <= 0.0:
        return None

    short_deltas = [abs(leg.delta) for leg in trial_income] or [
        abs(leg.delta) for leg in trial_protection
    ]
    pop = estimate_pop(short_deltas)
    # calendar_spread/diagonal_spread have no calibration entry (no clean
    # single-expiry BS walk-forward analog — see the learning-engine wiring
    # plan) and always fall back to the heuristic here regardless of mode;
    # wired through the same shared gate anyway so a future calibration
    # effort for these two types needs no second code path.
    avg_win, avg_loss, _ = calibrated_ev_inputs(
        strategy_type, "debit", 0.0, max_profit_1lot, max_loss_1lot
    )
    if compute_ev(pop, avg_win, avg_loss) <= 0.0:
        return None

    per_lot_greeks = to_rupees(
        _per_lot_greeks_two_expiry(near_chain, far_chain, trial_protection, trial_income, 1),
        near_chain.spot,
    )
    try:
        margin_1lot = await executor.get_order_margin_for_structure(
            trial_protection, trial_income, f"{strategy_type}-sizing-probe"
        )
    except Exception as exc:  # noqa: BLE001 — never trade sized on a fake number if this fails
        _LOG.error(
            "seller_desk_no_trade_reason strategy=%s reason=margin_fetch_failed detail=%s",
            strategy_type,
            exc,
        )
        return None
    if margin_1lot.total_margin <= 0.0 or margin_1lot.total_margin < max_loss_1lot:
        _LOG.error(
            "seller_desk_no_trade_reason strategy=%s reason=implausible_margin real=%.0f "
            "theoretical=%.0f -- refusing rather than trusting a parsing bug",
            strategy_type,
            margin_1lot.total_margin,
            max_loss_1lot,
        )
        return None
    sizing = size_position(
        options_capital,
        risk_per_trade_pct,
        max_loss_1lot,
        margin_per_lot=margin_1lot.total_margin,
        available_margin=available_margin,
        current_greeks=current_greeks,
        per_lot_greeks=per_lot_greeks,
        greeks_budget=greeks_budget,
        margin_safety_pct=margin_safety_pct,
    )
    if sizing.rejected or sizing.lots < 1:
        return None

    final = build_two_expiry_structure(
        strategy_type, near_chain, far_chain, sizing.lots, directional_bias
    )
    if final is None:
        return None
    return final[0], final[1], sizing.lots


class SellerDesk:
    """Ties chain data + every decision/execution/safety module into one
    per-underlying cycle, plus the standing safety-monitoring sweeps."""

    def __init__(
        self,
        selector: StrategySelector,
        event_calendar: EventCalendar,
        executor: OptionsLegExecutor,
        chain_fetcher: ChainFetcher,
        *,
        options_capital: float,
        risk_per_trade_pct: float,
        margin_safety_pct: float,
        greeks_budget: GreeksBudget,
        min_rom_pct: float,
        enabled_strategy_types: set[str] | None = None,
        repo: OptionsRepository | None = None,
        is_paper: bool = True,
        alt_executor: OptionsLegExecutor | None = None,
    ) -> None:
        self._selector = selector
        self._event_calendar = event_calendar
        self._executor = executor
        # Executor for the OTHER mode (live when the desk is paper, and vice versa), used
        # only by strategies whose own registry execution_mode differs from the desk's.
        self._alt_executor = alt_executor
        self._chain_fetcher = chain_fetcher
        self._options_capital = options_capital
        self._risk_per_trade_pct = risk_per_trade_pct
        self._margin_safety_pct = margin_safety_pct
        self._greeks_budget = greeks_budget
        self._min_rom_pct = min_rom_pct
        self._repo = repo
        self._is_paper = is_paper
        from src.options.structure_builder import implemented_strategy_types  # noqa: PLC0415

        # Only strategies BOTH marked enabled in options_settings.json AND
        # actually implemented by structure_builder are ever candidates —
        # config-enabled-but-unbuilt types (e.g. calendar_spread,
        # protective_put right now) are silently excluded here rather than
        # attempted and failing downstream.
        configured = (
            enabled_strategy_types
            if enabled_strategy_types is not None
            else implemented_strategy_types()
        )
        self._enabled_strategy_types = configured & implemented_strategy_types()
        self.positions: dict[str, OpenPosition] = {}
        # G9 LLM reviewer (config/global.json llm.options_g9) — lazily built on
        # first use in run_cycle(), same lazy-init pattern as shared["swing_g9_llm"]
        # in orchestrator.py. _g9_llm_loaded distinguishes "not yet attempted" from
        # "attempted, no provider available" (both leave _g9_llm at None).
        self._g9_llm: Any = None
        self._g9_llm_loaded = False
        self._g9_malformed = 0
        # Portfolio-level safety state (options_spec.md 11 / 11.5) -- see
        # sweep_portfolio_risk(). Realized-today is re-seeded from the DB on
        # restore so a restart can't reset the daily-loss breaker.
        self._realized_day: date | None = None
        self._realized_today = 0.0
        self._entries_halted_day: date | None = None
        self._margin_blocked = False
        self._gap_blocked = False
        # Per-STRATEGY daily-loss kill (config/capital.json options.max_loss_per_day_per_strategy,
        # 2026-10-07): today's realized loss per registry strategy (see
        # _registry_strategy_for), and which strategies are halted for today. Both are
        # re-derived from the DB on restore (restore_positions), same pattern as
        # _realized_today above -- never a silently-forgotten latch.
        self._realized_today_by_strategy: dict[str, float] = {}
        self._strategy_halted_day: dict[str, date] = {}
        # Latest market observations per underlying (from chains seen in run_cycle /
        # sweeps): spot, ATM IV, last regime, and the day's first spot/IV for the
        # end-of-day Greeks P&L attribution.
        self._spot: dict[str, float] = {}
        self._atm_iv: dict[str, float] = {}
        self._last_regime: dict[str, str] = {}
        self._day_ref: dict[str, tuple[date, float, float | None]] = {}
        self._attribution_day: date | None = None
        # options_intraday_short_premium runtime state (src/options/short_premium.py); all per-process, all
        # re-derivable (positions come back from the DB, attempts only throttle retries).
        self._short_premium_attempts: dict[date, int] = {}
        self._short_premium_missing: dict[str, int] = {}
        self._short_premium_late_alert: dict[str, bool] = {}
        self._short_premium_missed_day: date | None = None
        self._short_premium_entered_day: date | None = None
        self._short_premium_bg: set[Any] = set()  # background LLM-shadow tasks (kept referenced)
        # The expiry-day strangle runs on the same engine under its own name: its own throttle/once-a-day state.
        self._expiry_strangle_attempts: dict[date, int] = {}
        self._expiry_strangle_missed_day: date | None = None
        self._expiry_strangle_entered_day: date | None = None

    @property
    def enabled_strategy_types(self) -> frozenset[str]:
        return frozenset(self._enabled_strategy_types)

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    def _pos_is_paper(self, pos: OpenPosition) -> bool:
        return self._is_paper if pos.is_paper is None else bool(pos.is_paper)

    def _executor_for(self, is_paper: bool) -> Any:
        """Order executor for a mode; None when that mode is not wired (fail closed)."""
        return self._executor if is_paper == self._is_paper else self._alt_executor

    def strategy_is_paper(self, name: str, crm_ctx: Any) -> bool:
        """A desk-hosted strategy's own mode: its registry execution_mode (via
        crm_ctx.strategy_execution_mode), else the desk's mode."""
        mode = (getattr(crm_ctx, "strategy_execution_mode", None) or {}).get(name)
        if mode in ("paper", "live"):
            return bool(mode == "paper")
        return self._is_paper

    def available_margin(self, broker_available_cash: float | None = None) -> float:
        """Sizing's margin-gate input (options_spec.md §14 margin check).

        In paper mode, the real broker's free cash has nothing to do with
        the ring-fenced paper capital sleeve (options_capital) -- querying
        it live isn't just unnecessary, it can wrongly under/over-constrain
        sizing based on account state the paper sleeve was never meant to
        reflect (found live 2026-09-08: options.paper_mode=true was still
        calling broker.get_margins() every cycle). Simulate it instead as
        capital minus margin already committed to this desk's own open
        paper positions -- self-consistent with the sleeve, matches how
        options_capital is used everywhere else. In live mode the real
        broker's free cash IS the authoritative constraint (other segments'
        positions can draw it down too), so the caller-supplied real value
        is used unchanged."""
        if self._is_paper:
            committed = sum(p.entry_margin for p in self.positions.values())
            return max(0.0, self._options_capital - committed)
        return float(broker_available_cash) if broker_available_cash is not None else 0.0

    def paper_sleeve_margin(self) -> float:
        """Paper capital sleeve left for PAPER positions, regardless of the desk's own mode
        (a desk-hosted strategy may be paper while the desk is live)."""
        committed = sum(p.entry_margin for p in self.positions.values() if self._pos_is_paper(p))
        return max(0.0, self._options_capital - committed)

    def portfolio_greeks(self) -> PortfolioGreeks:
        """Sum of every open position's entry-time Greeks (per-lot x lots), on the
        RUPEE scale the budgets use (delta x spot/100, gamma x spot^2/100) —
        used as `current` in the pre-trade Greeks budget check. Entry-time,
        not live: positions don't retain refreshed per-leg Greeks. Each position
        is scaled by its own underlying's latest spot."""
        total = PortfolioGreeks()
        for pos in self.positions.values():
            rs = to_rupees(self._native_greeks(pos), self._spot_for(pos))
            total.net_delta += rs.net_delta
            total.net_gamma += rs.net_gamma
            total.net_theta += rs.net_theta
            total.net_vega += rs.net_vega
        return total

    @staticmethod
    def _native_greeks(pos: OpenPosition) -> PortfolioGreeks:
        g = pos.entry_greeks
        n = pos.lots
        return PortfolioGreeks(g.net_delta * n, g.net_gamma * n, g.net_theta * n, g.net_vega * n)

    def _spot_for(self, pos: OpenPosition) -> float:
        """Best available spot for a position: latest chain, else its entry spot, else
        the mean income-leg strike (a restored position before any chain is seen)."""
        spot = self._spot.get(pos.underlying) or pos.entry_spot
        if spot:
            return spot
        strikes = [leg.strike for leg in (pos.income_legs or pos.protection_legs)]
        return sum(strikes) / len(strikes) if strikes else 0.0

    def _observe_chain(self, underlying: str, chain: OptionChain, today: date) -> None:
        self._spot[underlying] = chain.spot
        iv = chain.atm_iv()
        if iv is not None:
            self._atm_iv[underlying] = iv
        ref = self._day_ref.get(underlying)
        if ref is None or ref[0] != today:
            self._day_ref[underlying] = (today, chain.spot, iv)

    async def restore_positions(self) -> int:
        """Reconstruct self.positions from options_positions rows written by
        a prior process (options_positions table existed unused until this
        wiring — a launcher restart used to lose all seller-desk position
        tracking). No-op when no repo was injected (e.g. DB unavailable)."""
        if self._repo is None:
            return 0
        rows = [
            r
            for r in await self._repo.load_open_positions(is_paper=None)
            if bool(r.get("is_paper", self._is_paper)) == self._is_paper
            or r.get("strategy_type") in HOSTED_OWN_MODE_TYPES
        ]
        for row in rows:
            raw_legs: Any = row["legs"]
            legs: dict[str, Any] = json.loads(raw_legs) if isinstance(raw_legs, str) else raw_legs
            raw_net_credit = row["net_credit"]
            net_credit = float(raw_net_credit) if raw_net_credit is not None else 0.0  # type: ignore[arg-type]
            raw_entry_margin = row["entry_margin"]
            position_id = str(row["position_id"])
            self.positions[position_id] = OpenPosition(
                position_id=position_id,
                underlying=str(row["underlying"]),
                strategy_type=str(row["strategy_type"]),
                direction="credit" if net_credit > 0 else "debit",
                protection_legs=[OptionLeg(**leg) for leg in legs.get("protection", [])],
                income_legs=[OptionLeg(**leg) for leg in legs.get("income", [])],
                entry_premium=net_credit,
                entry_margin=float(raw_entry_margin) if raw_entry_margin is not None else 0.0,  # type: ignore[arg-type]
                expiry=cast("date", row["expiry"]),
                entered_at=cast("datetime", row["opened_at"]),
                adjustments_made=int(row.get("adjustments_count", 0) or 0),  # type: ignore[call-overload]
                entry_regime=cast("str | None", row.get("entry_regime")),
                entry_atm_iv=_opt_float(row.get("entry_atm_iv")),
                entry_spot=_opt_float(row.get("entry_spot")),
                entry_short_delta=_opt_float(row.get("entry_short_delta")),
                was_breached=bool(row.get("was_breached", False)),
                peak_unrealized_pnl=_opt_float(row.get("peak_unrealized_pnl")),
                is_paper=(
                    None
                    if bool(row.get("is_paper", self._is_paper)) == self._is_paper
                    else bool(row.get("is_paper"))
                ),
                closed_legs=list(legs.get("closed", [])),
                meta=dict(legs.get("meta", {})),
                entry_greeks=PortfolioGreeks(
                    net_delta=float(row.get("entry_delta") or 0.0),  # type: ignore[arg-type]
                    net_gamma=float(row.get("entry_gamma") or 0.0),  # type: ignore[arg-type]
                    net_theta=float(row.get("entry_theta") or 0.0),  # type: ignore[arg-type]
                    net_vega=float(row.get("entry_vega") or 0.0),  # type: ignore[arg-type]
                ),
            )
        today = datetime.now(_IST).date()
        try:
            self._realized_today = await self._repo.realized_pnl_closed_on(
                today, is_paper=self._is_paper
            )
            self._realized_day = today
        except Exception as exc:  # noqa: BLE001 — breaker seed is best-effort
            _LOG.warning("seller_desk_realized_seed_failed err=%s", exc)
        try:
            await self._seed_strategy_daily_loss(today)
        except Exception as exc:  # noqa: BLE001 — breaker seed is best-effort
            _LOG.warning("seller_desk_strategy_realized_seed_failed err=%s", exc)
        if rows:
            _LOG.info("seller_desk_positions_restored count=%d", len(rows))
        return len(rows)

    async def reconcile_duplicate_positions(self) -> int:
        """DB-level backstop for the "at most max_strategies_per_index OPEN
        strategies per index" invariant that run_cycle's in-memory guard enforces going
        forward -- called once per orchestrator cycle, before any underlying
        is evaluated for a new trade. Catches the case the in-memory guard
        can't: self.positions and the DB having drifted apart (e.g. a process
        running stale pre-guard code, as happened 2026-09-11 -- 60 duplicate
        NIFTY positions accumulated over 6.5 hours before anyone noticed).
        No-op when no repo was injected. Returns the number deleted."""
        if self._repo is None:
            return 0
        deleted_ids = await self._repo.delete_duplicate_open_positions(
            is_paper=self._is_paper,
            keep_per_underlying=max(1, _seller_settings.max_strategies_per_index()),
            exclude_strategy_types=sorted(HOSTED_STRATEGY_TYPES),
        )
        for position_id in deleted_ids:
            self.positions.pop(position_id, None)
        if deleted_ids:
            _LOG.warning(
                "seller_desk_duplicates_auto_deleted count=%d ids=%s",
                len(deleted_ids),
                deleted_ids,
            )
        return len(deleted_ids)

    async def run_cycle(
        self,
        underlying: str,
        *,
        regime: str,
        ivr: float | None,
        term_structure: str | None,
        today: date,
        now: datetime,
        expiry_calendar: dict[str, list[date]],
        available_margin: float,
        is_iron_condor_hint: bool = False,
        directional_bias: float | None = None,
        vix: float | None = None,
        crm: CentralRiskManager | None = None,
        crm_ctx: CRMContext | None = None,
    ) -> OpenPosition | None:
        """One full decision cycle for one underlying. Returns the opened
        position, or None if nothing qualified / everything was gated out.

        Every candidate structure that clears sizing is still routed through
        ``crm.evaluate()`` (src/risk/crm.py G0-G10) before any order is
        placed, via src/options/seller_crm_adapter.py -- same risk pipeline
        every other live-trading engine in this system goes through.
        ``crm``/``crm_ctx`` are required for a live cycle to place anything;
        if either is missing this method fails closed (skips the trade,
        never silently bypasses CRM).

        Each candidate strategy type gets its OWN chain, fetched at its own
        DTE band (options_spec.md §6) — an Iron Condor (30-45 DTE) and an
        Iron Butterfly (14-21 DTE) are never evaluated against the same
        nearest-expiry chain. Bug fix: an earlier version reused one
        nearest-expiry chain for every candidate regardless of its target
        DTE band, which would have entered every strategy off whatever
        expiry happened to be nearest — often a weekly, far too close to
        expiry for e.g. an Iron Condor.

        The pre-entry DTE check itself is against a freshly fetched REAL
        chain's expiry, not the nse_expiry_calendar.json placeholder --
        found live 2026-09-10: that file is explicitly flagged as
        unverified weekday-generated data (still lists BANKNIFTY/FINNIFTY
        as weekly) and disagrees with the real NFO instrument dump (both
        actually ~monthly), which let this check wrongly ALLOW entry
        against a near-dated expiry that doesn't exist, only for every
        real per-strategy chain fetch below to then correctly reject.
        """
        self._last_regime[underlying] = regime
        nearest_chain = await self._chain_fetcher.fetch_tier1(underlying)
        if nearest_chain is None:
            _LOG.info(
                "seller_desk_pre_entry_blocked underlying=%s reason=no_chain_data", underlying
            )
            return None
        self._observe_chain(underlying, nearest_chain, today)
        # The min-entry-DTE gate is applied per strategy below, against the
        # expiry that strategy would actually trade -- NOT the nearest expiry
        # here. Gating on the nearest (a NIFTY weekly 0-1 DTE on Mon/Tue) blocked
        # NIFTY outright even though every candidate trades a 10+ DTE expiry.

        # self.positions only ever holds currently-open positions (removed on
        # close in _close_position, restored from options_positions on
        # restart) -- a plain underlying match is enough to know one is
        # already open. Without this, every 5-min loop cycle re-evaluated and
        # opened ANOTHER structure on the same underlying: 4 identical
        # bull_put_spread positions were opened on NIFTY within ~19 minutes
        # because nothing here ever consulted self.positions before building
        # a new one.
        _per_und = _seller_settings.max_strategies_per_index()
        # The expiry-day fly has its own slot (2026-10-09): it never uses up a desk slot.
        _open_here = sum(
            1
            for p in self.positions.values()
            if p.underlying == underlying and p.strategy_type != EXPIRY_DAY_TYPE
        )
        if _per_und > 0 and _open_here >= _per_und:
            _LOG.info(
                "seller_desk_already_open underlying=%s open_strategies=%d "
                "max_strategies_per_index=%d",
                underlying,
                _open_here,
                _per_und,
            )
            return None

        blocked_reason = self.entries_blocked_reason(today, available_margin)
        if blocked_reason is not None:
            _LOG.warning(
                "seller_desk_entries_blocked underlying=%s reason=%s", underlying, blocked_reason
            )
            return None
        _strat_halted = self.strategy_halted_reason(DESK_OWN_STRATEGY_NAME, today)
        if _strat_halted is not None:
            _LOG.warning(
                "seller_desk_entries_blocked underlying=%s reason=%s", underlying, _strat_halted
            )
            return None
        executor = self._executor_for(self._is_paper)
        if executor is None:
            _LOG.error(
                "seller_desk_no_executor_for_mode underlying=%s mode=%s -- refusing (that "
                "mode's broker is not wired)",
                underlying,
                "paper" if self._is_paper else "live",
            )
            return None

        candidates = [
            c
            for c in self._selector.candidates(
                regime, ivr, term_structure=term_structure, directional_bias=directional_bias
            )
            if c in self._enabled_strategy_types
        ]
        blackout = await self._event_calendar.selling_blocked(
            underlying, now, is_iron_condor=is_iron_condor_hint
        )
        if blackout.blocked:
            from src.options.strategy_selector import is_debit_type  # noqa: PLC0415

            candidates = [c for c in candidates if is_debit_type(c)]
            _LOG.info(
                "seller_desk_selling_blocked underlying=%s reason=%s", underlying, blackout.reason
            )

        _LOG.info(
            "seller_desk_cycle_start underlying=%s regime=%s ivr=%s candidates=%s",
            underlying,
            regime,
            f"{ivr:.1f}" if ivr is not None else "None",
            candidates,
        )

        best: tuple[str, list[OptionLeg], list[OptionLeg], int, OptionChain] | None = None
        best_score = float("-inf")
        for strategy_type in candidates:
            same_side = self._same_direction_open(strategy_type)
            if same_side:
                _LOG.info(
                    "seller_desk_no_trade_reason strategy=%s reason=correlated_same_direction_open "
                    "underlying=%s open=%s",
                    strategy_type,
                    underlying,
                    same_side,
                )
                continue
            if strategy_type in _TWO_EXPIRY_TYPES:
                expiries = select_calendar_expiries(today, expiry_calendar, underlying)
                if expiries is None:
                    continue
                sell_expiry, buy_expiry = expiries
                near_dte = compute_dte(sell_expiry, today)
                far_dte = compute_dte(buy_expiry, today)
                tier2 = await self._chain_fetcher.fetch_tier2(underlying, [near_dte, far_dte])
                near_chain = tier2.get(near_dte)
                far_chain = tier2.get(far_dte)
                if near_chain is None or far_chain is None:
                    continue
                allowed, reason = pre_entry_dte_check_from_expiry(near_chain.expiry.date(), today)
                if not allowed:
                    _LOG.info(
                        "seller_desk_pre_entry_blocked underlying=%s strategy=%s reason=%s",
                        underlying,
                        strategy_type,
                        reason,
                    )
                    continue
                sized = await evaluate_and_size_two_expiry(
                    strategy_type=strategy_type,
                    near_chain=near_chain,
                    far_chain=far_chain,
                    options_capital=self._options_capital,
                    risk_per_trade_pct=self._risk_per_trade_pct,
                    margin_safety_pct=self._margin_safety_pct,
                    available_margin=available_margin,
                    current_greeks=self.portfolio_greeks(),
                    greeks_budget=self._greeks_budget,
                    directional_bias=directional_bias,
                    executor=executor,
                )
                if sized is None:
                    continue
                protection_legs, income_legs, lots = sized
                _, _, net_premium, _direction = estimate_economics(
                    strategy_type, protection_legs, income_legs, near_chain.lot_size, lots
                )
                score = abs(net_premium)
                if score > best_score:
                    best_score = score
                    best = (strategy_type, protection_legs, income_legs, lots, near_chain)
                continue

            from src.options.expiry_selector import dte_band  # noqa: PLC0415

            lo_dte, hi_dte = dte_band(strategy_type, ivr=ivr)
            tier2 = await self._chain_fetcher.fetch_tier2(underlying, [lo_dte])
            strategy_chain = tier2.get(lo_dte)
            if strategy_chain is None:
                _LOG.info(
                    "seller_desk_no_trade_reason strategy=%s reason=no_chain_for_dte_bucket "
                    "underlying=%s lo_dte=%d",
                    strategy_type,
                    underlying,
                    lo_dte,
                )
                continue
            strategy_dte = compute_dte(strategy_chain.expiry.date(), today)
            if not (lo_dte <= strategy_dte <= hi_dte):
                # The nearest expiry >= lo_dte overshot the band entirely
                # (e.g. next available expiry is 60 DTE for a 30-45 band).
                _LOG.info(
                    "seller_desk_no_trade_reason strategy=%s reason=expiry_out_of_band "
                    "underlying=%s nearest_expiry_dte=%d band=(%d,%d)",
                    strategy_type,
                    underlying,
                    strategy_dte,
                    lo_dte,
                    hi_dte,
                )
                continue
            allowed, reason = pre_entry_dte_check_from_expiry(strategy_chain.expiry.date(), today)
            if not allowed:
                _LOG.info(
                    "seller_desk_pre_entry_blocked underlying=%s strategy=%s reason=%s",
                    underlying,
                    strategy_type,
                    reason,
                )
                continue

            size_mult = sizing_multiplier(strategy_type, ivr, regime=regime)
            if size_mult != 1.0:
                _LOG.info(
                    "seller_desk_reduced_size underlying=%s strategy=%s ivr=%.1f mult=%.2f",
                    underlying,
                    strategy_type,
                    ivr if ivr is not None else float("nan"),
                    size_mult,
                )
            sized = await evaluate_and_size(
                strategy_type=strategy_type,
                chain=strategy_chain,
                options_capital=self._options_capital,
                risk_per_trade_pct=self._risk_per_trade_pct * size_mult,
                margin_safety_pct=self._margin_safety_pct,
                available_margin=available_margin,
                current_greeks=self.portfolio_greeks(),
                greeks_budget=self._greeks_budget,
                dte_value=strategy_dte,
                min_rom_pct=self._min_rom_pct,
                executor=executor,
                ivr=ivr,
                full_risk_per_trade_pct=self._risk_per_trade_pct,
            )
            if sized is None:
                continue
            protection_legs, income_legs, lots = sized
            _, _, net_premium, _direction = estimate_economics(
                strategy_type, protection_legs, income_legs, strategy_chain.lot_size, lots
            )
            score = abs(net_premium)  # simplest ranking: largest real premium among qualifiers
            if score > best_score:
                best_score = score
                best = (strategy_type, protection_legs, income_legs, lots, strategy_chain)

        if best is None:
            _LOG.info("seller_desk_cycle_no_trade underlying=%s", underlying)
            return None
        strategy_type, protection_legs, income_legs, lots, chain = best

        # Computed up-front (pure math, no I/O) so CRM sees the structure's
        # real pre-trade economics before any order is placed.
        max_loss, max_profit, net_premium, direction = estimate_economics(
            strategy_type, protection_legs, income_legs, chain.lot_size, lots
        )
        _risk_blocked = self.shared_risk_ceiling_reason(max_loss)
        if _risk_blocked is not None:
            _LOG.info(
                "seller_desk_no_trade_reason strategy=%s reason=%s", strategy_type, _risk_blocked
            )
            return None
        signal_id = f"SELLER-{uuid.uuid4().hex[:10].upper()}"

        if crm is None or crm_ctx is None:
            # Fail closed: no CRM wired into this call must never mean "skip
            # risk gating" -- that is the exact bypass this retrofit fixes.
            _LOG.warning(
                "seller_desk_crm_unavailable underlying=%s strategy=%s -- skipping cycle "
                "(no crm/crm_ctx passed to run_cycle)",
                underlying,
                strategy_type,
            )
            return None

        crm_signal = build_seller_crm_signal(
            signal_id=signal_id,
            underlying=underlying,
            strategy_type=strategy_type,
            lots=lots,
            max_loss=max_loss,
            max_profit=max_profit,
            regime=regime,
            vix=vix if vix is not None else 15.0,
        )

        if not self._crm_precheck(crm, crm_signal, crm_ctx, lots, underlying):
            return None
        if not await self._g9_review(
            crm_signal,
            crm_ctx,
            underlying=underlying,
            strategy_type=strategy_type,
            regime=regime,
            max_loss=max_loss,
            max_profit=max_profit,
            lots=lots,
            vix=vix,
        ):
            return None

        self._stamp_open_strategies(crm_ctx)
        decision = crm.evaluate(crm_signal, crm_ctx)
        log_gate_decisions(crm_signal, decision)
        if not decision.approved or decision.final_quantity <= 0:
            _LOG.info(
                "seller_desk_crm_blocked underlying=%s strategy=%s gate=%s reason=%s",
                underlying,
                strategy_type,
                decision.blocking_gate,
                decision.blocking_reason,
            )
            return None
        if decision.final_quantity < lots:
            # G8 wants to size this down (e.g. remaining daily risk budget <
            # the full structure). The legs here are already built for
            # `lots` -- resizing a built multi-leg structure to fewer lots
            # isn't a simple quantity edit (strikes/legs would need
            # rebuilding), so this cycle is skipped rather than partially
            # executed at an untested size. A structure_builder-aware resize
            # is a candidate follow-up, not required for this phase.
            _LOG.info(
                "seller_desk_crm_sized_down underlying=%s strategy=%s crm_qty=%d built_lots=%d "
                "-- skipping (full-size structure only)",
                underlying,
                strategy_type,
                decision.final_quantity,
                lots,
            )
            return None

        opened = await self._execute_and_record(
            underlying=underlying,
            strategy_type=strategy_type,
            protection_legs=protection_legs,
            income_legs=income_legs,
            lots=lots,
            chain=chain,
            net_premium=net_premium,
            max_loss=max_loss,
            max_profit=max_profit,
            direction=direction,
            signal_id=signal_id,
            regime=regime,
            today=today,
            now=now,
        )
        if opened is not None and strategy_type in _WIDTH_CAPPED_TYPES:
            self._schedule_direction_shadow(
                opened, chain, regime=regime, ivr=ivr, directional_bias=directional_bias, vix=vix
            )
        return opened

    def _schedule_direction_shadow(
        self,
        pos: OpenPosition,
        chain: OptionChain,
        *,
        regime: str,
        ivr: float | None,
        directional_bias: float | None,
        vix: float | None,
    ) -> None:
        """Log an LLM expiry-direction forecast beside a just-opened credit spread (research only,
        never gates or delays the trade -- see direction_shadow.py)."""
        if not _seller_settings.direction_shadow().get("enabled", False) or not pos.income_legs:
            return
        import asyncio  # noqa: PLC0415

        from src.options import direction_shadow  # noqa: PLC0415

        ctx = direction_shadow.build_context(
            self,
            underlying=pos.underlying,
            strategy_type=pos.strategy_type,
            regime=regime,
            ivr=ivr,
            directional_bias=directional_bias,
            vix=vix,
            short_strike=pos.income_legs[0].strike,
            spot=chain.spot,
            expiry=pos.expiry.isoformat(),
            dte=chain.days_to_expiry,
        )
        task = asyncio.create_task(direction_shadow.record_forecast(self, ctx, pos.position_id))
        self._short_premium_bg.add(task)
        task.add_done_callback(self._short_premium_bg.discard)

    def _stamp_open_strategies(self, crm_ctx: Any) -> None:
        """Tell CRM G7 how many option STRATEGIES are open in this desk's mode. Before
        2026-10-06 nothing ever filled this count, so G7 always saw 0 and the
        max_open_strategies cap (then max_open_options_positions) never blocked."""
        counts = getattr(crm_ctx, "mode_engine_open_positions", None)
        if isinstance(counts, dict):
            for mode_paper in (True, False):
                counts[f"{'paper' if mode_paper else 'live'}:options"] = sum(
                    1
                    for p in self.positions.values()
                    if self._pos_is_paper(p) == mode_paper
                    and p.strategy_type not in (EXPIRY_DAY_TYPE, SHORT_PREMIUM_TYPE)
                )
        self._stamp_daily_loss(crm_ctx)

    def _stamp_daily_loss(self, crm_ctx: Any) -> None:
        """Tell CRM G1/G8 today's REALIZED options loss, in this desk's own mode -- a
        magnitude (0 when the day is net flat or positive, never negative). Distinct from
        and a lighter-weight companion to sweep_portfolio_risk's daily_loss_halt_pct,
        which also counts open mark-to-market and is checked every sweep cycle, not just
        at entry time (options_settings.json portfolio_risk block's own note already
        describes this split). Before 2026-10-06 this field was never written, so G1/G8
        always read a stuck Rs 0 and position_limits.max_daily_loss_rupees behaved as a
        flat per-TRADE ceiling instead of a running per-DAY one -- found when a Rs 5,000
        value (meant as a day-level stop) blocked every strategy outright, since even one
        trade's own max loss (Rs 1,700-19,000 depending on strategy) exceeded it."""
        if hasattr(crm_ctx, "options_daily_loss_so_far"):
            crm_ctx.options_daily_loss_so_far = max(0.0, -self._realized_today)

    def _crm_precheck(
        self, crm: Any, crm_signal: Any, crm_ctx: Any, lots: int, underlying: str
    ) -> bool:
        """Run every CRM gate except G9 BEFORE asking the LLM: if a deterministic gate
        (daily budget, caps, time, regime...) will refuse anyway, skip the LLM call.
        True = proceed to G9. Doubles without precheck_without_llm always proceed."""
        pre = getattr(crm, "precheck_without_llm", None)
        if pre is None:
            return True
        self._stamp_open_strategies(crm_ctx)
        d = pre(crm_signal, crm_ctx)
        if d.approved and d.final_quantity >= lots:
            return True
        _LOG.info(
            "seller_desk_crm_precheck_blocked underlying=%s strategy=%s gate=%s reason=%s "
            "-- LLM not called",
            underlying,
            crm_signal.strategy_name,
            d.blocking_gate,
            d.blocking_reason,
        )
        return False

    async def _g9_review(
        self,
        crm_signal: Any,
        crm_ctx: CRMContext,
        *,
        underlying: str,
        strategy_type: str,
        regime: str,
        max_loss: float,
        max_profit: float,
        lots: int,
        vix: float | None,
    ) -> bool:
        """G9 LLM confirmation. False = skip this cycle (gate required, no verdict).

        Required when strategies_registry.json `llm_gate_required` is true for this
        strategy (via crm_ctx.llm_gate_required); providers/timeout in
        config/global.json llm.options_g9. Populates meta so the BLOCKING G9 gate
        inside crm.evaluate() can read it. entry_price/stop_loss_pct/target_pct are
        NOT passed to the prompt: build_seller_crm_signal's docstring is explicit
        that those are synthetic CRM-sizing placeholders, not this structure's real
        economics — max_loss/max_profit/lots below are."""
        if not crm_ctx.llm_gate_required.get(registry_name_of(crm_signal), False):
            return True
        from src.llm.providers import g9_chain_timeout, make_options_gate_llm_client

        if not self._g9_llm_loaded:
            self._g9_llm = make_options_gate_llm_client()
            self._g9_llm_loaded = True
        _verdict: dict[str, Any] | None = None
        if self._g9_llm is not None:
            from src.llm.trade_evaluator import evaluate_trade_gated as _evaluate_trade

            # No news_context: run_cycle() isn't given the orchestrator's
            # shared dict (news_recent lives there), unlike the swing/intraday
            # G9 calls — the LLM still reviews on regime/vix/economics alone.
            _verdict = await _evaluate_trade(
                self._g9_llm,
                {
                    "symbol": underlying,
                    "direction": "SELL",
                    "strategy_name": crm_signal.strategy_name,
                    "regime": regime,
                    "max_loss": max_loss,
                    "max_profit": max_profit,
                    "lots": lots,
                    "strategy_health": "HEALTHY",
                    "vix": vix if vix is not None else 15.0,
                },
                self._g9_malformed,
                chain_timeout_secs=g9_chain_timeout("options_g9", 30.0),
            )
            self._g9_malformed = _verdict["malformed"]
            crm_signal.meta["llm_confidence"] = _verdict["llm_confidence"]
            crm_signal.meta["llm_flag"] = _verdict["llm_flag"]
            crm_signal.meta["llm_reason"] = _verdict["llm_reason"]
            crm_signal.meta["llm_evaluated"] = _verdict["evaluated"]
            crm_signal.meta["llm_provider"] = _verdict.get("provider")
            if _verdict["evaluated"]:
                _LOG.info(
                    "options_g9_check underlying=%s strategy=%s flag=%s confidence=%.2f "
                    "provider=%s attempts=%s",
                    underlying,
                    strategy_type,
                    _verdict["llm_flag"],
                    _verdict["llm_confidence"],
                    _verdict.get("provider"),
                    _verdict.get("attempts"),
                )
        if not crm_signal.meta.get("llm_evaluated", False):
            _LOG.warning(
                "seller_desk_g9_no_verdict underlying=%s strategy=%s — LLM gate required, "
                "cycle skipped (%s)",
                underlying,
                strategy_type,
                "no provider configured"
                if self._g9_llm is None
                else str((_verdict or {}).get("llm_reason", "no valid verdict")),
            )
            return False
        return True

    async def _g9_shadow(
        self,
        crm_signal: Any,
        *,
        underlying: str,
        regime: str,
        max_loss: float,
        max_profit: float,
        lots: int,
        vix: float | None,
    ) -> dict[str, Any] | None:
        """NON-blocking LLM verdict for a strategy whose gate is off: same prompt/providers as
        _g9_review, but the verdict is only returned (and logged), never written to the signal's
        llm_* meta, so CRM G9 cannot act on it. Lets paper trades be compared with and without the
        LLM without alternating weeks. None when no provider answered."""
        from src.llm.providers import (  # noqa: PLC0415
            g9_chain_timeout,
            make_options_gate_llm_client,
        )

        if not self._g9_llm_loaded:
            self._g9_llm = make_options_gate_llm_client()
            self._g9_llm_loaded = True
        if self._g9_llm is None:
            return None
        from src.llm.trade_evaluator import evaluate_trade_gated as _evaluate_trade  # noqa: PLC0415

        verdict = await _evaluate_trade(
            self._g9_llm,
            {
                "symbol": underlying,
                "direction": "SELL",
                "strategy_name": crm_signal.strategy_name,
                "regime": regime,
                "max_loss": max_loss,
                "max_profit": max_profit,
                "lots": lots,
                "strategy_health": "HEALTHY",
                "vix": vix if vix is not None else 15.0,
            },
            self._g9_malformed,
            chain_timeout_secs=g9_chain_timeout("options_g9", 30.0),
        )
        self._g9_malformed = verdict["malformed"]
        if not verdict["evaluated"]:
            return None
        out = {
            "flag": verdict["llm_flag"],
            "confidence": verdict["llm_confidence"],
            "reason": verdict["llm_reason"],
            "provider": verdict.get("provider"),
        }
        _LOG.info(
            "options_g9_shadow strategy=%s flag=%s confidence=%.2f provider=%s",
            crm_signal.strategy_name,
            out["flag"],
            float(out["confidence"]),
            out["provider"],
        )
        return out

    async def _execute_and_record(
        self,
        *,
        underlying: str,
        strategy_type: str,
        protection_legs: list[OptionLeg],
        income_legs: list[OptionLeg],
        lots: int,
        chain: OptionChain,
        net_premium: float,
        max_loss: float,
        max_profit: float,
        direction: str,
        signal_id: str,
        regime: str,
        today: date,
        now: datetime,
        is_paper: bool | None = None,
        skip_shared_risk: bool = False,
    ) -> OpenPosition | None:
        """Place a CRM-approved structure (protection first), record every attempt,
        track + persist the opened position, and notify. Shared by run_cycle and
        run_weekly_condor_cycle so both use one execution path."""
        mode_paper = self._is_paper if is_paper is None else is_paper
        executor = self._executor_for(mode_paper)
        if executor is None:
            _LOG.error(
                "seller_desk_no_executor_for_mode underlying=%s strategy=%s mode=%s -- "
                "refusing (that mode's broker is not wired)",
                underlying,
                strategy_type,
                "paper" if mode_paper else "live",
            )
            return None
        try:
            margin_result = await executor.get_order_margin_for_structure(
                protection_legs, income_legs, signal_id
            )
        except Exception as exc:  # noqa: BLE001 — never execute on a fake margin number
            _LOG.error(
                "seller_desk_margin_fetch_failed underlying=%s strategy=%s signal=%s err=%s",
                underlying,
                strategy_type,
                signal_id,
                exc,
            )
            try:
                notify(
                    EventKind.STRATEGY_ERROR,
                    is_paper=mode_paper,
                    symbol=underlying,
                    segment=Segment.OPTIONS,
                    strategy=strategy_type,
                    reason=f"real margin fetch failed: {exc}",
                )
            except Exception:  # notify() must never break the trade path
                pass
            return None
        if margin_result.total_margin <= 0.0 or margin_result.total_margin < max_loss:
            _LOG.error(
                "seller_desk_margin_implausible underlying=%s strategy=%s real=%.0f "
                "theoretical=%.0f -- refusing rather than trusting a parsing bug",
                underlying,
                strategy_type,
                margin_result.total_margin,
                max_loss,
            )
            try:
                notify(
                    EventKind.STRATEGY_ERROR,
                    is_paper=mode_paper,
                    symbol=underlying,
                    segment=Segment.OPTIONS,
                    strategy=strategy_type,
                    reason=(
                        f"implausible real margin {margin_result.total_margin:.0f} < "
                        f"theoretical max_loss {max_loss:.0f} -- treating as a parsing bug"
                    ),
                )
            except Exception:  # notify() must never break the trade path
                pass
            return None
        real_risk_blocked = None if skip_shared_risk else self.shared_risk_ceiling_reason(max_loss)
        if real_risk_blocked is not None:
            _LOG.info(
                "seller_desk_execution_blocked_by_real_margin underlying=%s strategy=%s "
                "reason=%s real_margin=%.0f theoretical_max_loss=%.0f",
                underlying,
                strategy_type,
                real_risk_blocked,
                margin_result.total_margin,
                max_loss,
            )
            return None
        result = await executor.execute_structure(protection_legs, income_legs, signal_id)
        if self._repo is not None:  # every attempt, including the failed ones, is recorded
            try:
                await self._repo.record_execution(
                    result,
                    protection_legs,
                    income_legs,
                    result.position_id in executor.naked_position_ids(),
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.error(
                    "options_execution_log_write_failed position=%s err=%s", result.position_id, exc
                )
        if (
            strategy_type == SHORT_PREMIUM_TYPE
            or (
                strategy_type == EXPIRY_DAY_TYPE and not protection_legs
            )  # strangle; the fly has wings
        ) and result.outcome == "PARTIAL_DEGRADED":
            # Both legs or nothing: a lone filled short leg is an unplanned directional position.
            for state in result.protection_legs + result.income_legs:
                if state.filled:
                    await executor.close_leg(state.leg, result.position_id)
            _LOG.error(
                "options_intraday_short_premium_partial_fill_flattened position=%s -- one leg did not fill, "
                "the filled leg(s) were closed",
                result.position_id,
            )
            return None
        if result.outcome not in ("ALL_FILLED", "PARTIAL_DEGRADED"):
            _LOG.warning(
                "seller_desk_execution_failed underlying=%s outcome=%s", underlying, result.outcome
            )
            return None

        position = OpenPosition(
            position_id=result.position_id,
            underlying=underlying,
            strategy_type=strategy_type,
            direction=direction,
            protection_legs=protection_legs,
            income_legs=income_legs,
            entry_premium=net_premium,
            entry_margin=margin_result.total_margin,
            expiry=chain.expiry.date(),
            entered_at=now,
            entry_greeks=_per_lot_greeks(chain, protection_legs, income_legs, lots),
            entry_regime=regime,
            entry_atm_iv=chain.atm_iv(),
            entry_spot=chain.spot,
            entry_short_delta=abs(income_legs[0].delta) if income_legs else None,
            is_paper=None if mode_paper == self._is_paper else mode_paper,
        )
        self.positions[position.position_id] = position
        if self._repo is not None:
            try:
                await self._repo.insert_position(
                    position.position_id,
                    underlying=underlying,
                    strategy_type=strategy_type,
                    protection_legs=protection_legs,
                    income_legs=income_legs,
                    expiry=position.expiry,
                    dte_at_entry=compute_dte(position.expiry, today),
                    lot_size_at_entry=chain.lot_size,
                    net_credit=net_premium,
                    max_loss=max_loss,
                    max_profit=max_profit,
                    entry_margin=margin_result.total_margin,
                    entry_greeks=position.entry_greeks,
                    opened_at=now,
                    is_paper=mode_paper,
                    entry_regime=position.entry_regime,
                    entry_atm_iv=position.entry_atm_iv,
                    entry_spot=position.entry_spot,
                    entry_short_delta=position.entry_short_delta,
                )
            except Exception as exc:  # noqa: BLE001 — persistence is best-effort, never blocks a live position
                _LOG.warning(
                    "seller_desk_position_persist_failed id=%s err=%s", position.position_id, exc
                )
        _LOG.info(
            "seller_desk_position_opened id=%s underlying=%s type=%s lots=%d premium=%.0f",
            position.position_id,
            underlying,
            strategy_type,
            lots,
            net_premium,
        )
        try:
            notify(
                EventKind.ENTRY_FILL,
                is_paper=mode_paper,
                agent_trade_id=position.position_id,
                symbol=underlying,
                segment=Segment.OPTIONS,
                dedupe_key=f"entry:{position.position_id}",
                direction=strategy_type,
                qty=lots,
                fill=net_premium,
                sl=max_loss,
                target=max_profit,
                product=strategy_type,
            )
        except Exception:  # notify() must never fail the trade path
            pass
        return position

    async def run_weekly_condor_cycle(
        self,
        underlying: str,
        *,
        today: date,
        now: datetime,
        available_margin: float,
        regime: str,
        vix: float | None,
        crm: CentralRiskManager | None,
        crm_ctx: CRMContext | None,
    ) -> OpenPosition | None:
        """Hypothesis B -- one defined-risk NIFTY iron condor per weekly expiry
        (docs/hypotheses/B_nifty_weekly_condor_simple.md; config weekly_condor_simple).

        Entered only on the FIRST trading day of the expiry cycle inside the entry
        window, at most once per expiry (DB-checked, survives restarts and early
        exits). Shares every desk risk control: paper hard-lock, daily-loss/margin/
        gap entry blocks, desk min-entry-DTE, event blackout, size_position's
        margin + Greeks budget, ROM gate, G9 LLM (under its OWN registry name), CRM
        G0-G10, and the shared protection-first execution path. Skips the desk's
        IVR/regime menu and crude-POP EV gate -- those select the desk's own
        structures; this structure's edge was evaluated separately."""
        cfg = _seller_settings.weekly_condor()
        if not cfg.get("enabled", False):
            return None
        name = str(cfg.get("registry_name", "nifty_weekly_condor_simple"))
        tag = f"weekly_condor underlying={underlying}"
        if crm is None or crm_ctx is None:
            _LOG.warning("%s skipped reason=crm_unavailable (fail-closed)", tag)
            return None
        mode_paper = self.strategy_is_paper(name, crm_ctx)
        executor = self._executor_for(mode_paper)
        if executor is None:
            _LOG.error(
                "%s skipped reason=mode_not_wired mode=%s (registry execution_mode) -- no "
                "executor for that mode; refusing",
                tag,
                "paper" if mode_paper else "live",
            )
            return None
        win = cfg.get("entry_window", ["10:00", "10:30"])
        start, end = (datetime.strptime(t, "%H:%M").time() for t in win)
        if not (start <= now.time() <= end):
            return None
        if any(
            p.strategy_type == WEEKLY_CONDOR_TYPE and p.underlying == underlying
            for p in self.positions.values()
        ):
            _LOG.info("%s skipped reason=already_open", tag)
            return None
        # 2026-10-07: removed a max_strategies_per_index check against self.positions --
        # this strategy's own spec (options_settings.json weekly_condor_simple) explicitly
        # promises "its own one-per-underlying slot (does not block or get deleted by the
        # desk's NIFTY position)", but this counted the desk's OWN structures against the
        # SAME cap, starving the condor the moment the desk had 2 of its own NIFTY spreads
        # open -- found live 2026-10-07 (desk at 2/2 NIFTY, condor blocked). The already_open
        # check above already guarantees at most one condor per underlying; that IS this
        # strategy's own slot, independent of how many desk structures are open.
        blocked_reason = self.entries_blocked_reason(today, available_margin)
        if blocked_reason is not None:
            _LOG.warning("%s skipped reason=entries_blocked detail=%s", tag, blocked_reason)
            return None
        _strat_halted = self.strategy_halted_reason(name, today)
        if _strat_halted is not None:
            _LOG.warning("%s skipped reason=%s", tag, _strat_halted)
            return None

        chain = await self._chain_fetcher.fetch_tier1(underlying)
        if chain is None:
            _LOG.info("%s skipped reason=no_chain_data", tag)
            return None
        self._observe_chain(underlying, chain, today)
        expiry = chain.expiry.date()
        first_day = first_trading_day_of_cycle(expiry)
        # 2026-10-07: was `today != first_day` -- exactly ONE calendar day, inside a
        # single ~20-30min window, to catch an entire week's trade. A restart, a Kite
        # hiccup or any transient failure on that one day silently loses the whole
        # cycle with no way to catch up (found live: zero entries across this
        # strategy's first several days). has_position_for_expiry below already
        # guarantees at most one entry per expiry, so widening this to "first_day or
        # later" only adds resilience, never a double-entry risk. min_entry_dte below
        # still stops it entering too close to expiry if every earlier day was missed.
        if today < first_day:
            _LOG.info(
                "%s skipped reason=cycle_not_started_yet expiry=%s first_day=%s",
                tag,
                expiry,
                first_day,
            )
            return None
        allowed, dte_reason = pre_entry_dte_check_from_expiry(expiry, today)
        if not allowed:
            _LOG.info("%s skipped reason=min_entry_dte detail=%s", tag, dte_reason)
            return None
        if self._repo is not None and await self._repo.has_position_for_expiry(
            strategy_type=WEEKLY_CONDOR_TYPE,
            underlying=underlying,
            expiry=expiry,
            is_paper=mode_paper,
        ):
            _LOG.info("%s skipped reason=already_traded_this_expiry expiry=%s", tag, expiry)
            return None
        blackout = await self._event_calendar.selling_blocked(underlying, now)
        if blackout.blocked:
            _LOG.info("%s skipped reason=event_blackout detail=%s", tag, blackout.reason)
            return None

        built = build_weekly_condor(
            chain,
            1,
            short_delta=float(cfg.get("short_delta", 0.15)),
            wing_points=float(cfg.get("wing_points", 200)),
        )
        if built is None:
            _LOG.info("%s skipped reason=structure_build_failed (strikes/liquidity)", tag)
            return None
        prot1, inc1 = built
        wing = float(cfg.get("wing_points", 200))
        credit_1lot = _net_premium(prot1, inc1)
        max_loss_1lot = wing * chain.lot_size - credit_1lot
        if credit_1lot <= 0.0 or max_loss_1lot <= 0.0:
            _LOG.info(
                "%s skipped reason=degenerate_economics credit=%.0f max_loss=%.0f",
                tag,
                credit_1lot,
                max_loss_1lot,
            )
            return None
        dte_value = compute_dte(expiry, today)
        passed_rom, rom_reason = rom_gate(
            credit_1lot, max_loss_1lot, dte_value, min_pct=self._min_rom_pct
        )
        if not passed_rom:
            _LOG.info("%s skipped reason=rom_gate_failed detail=%s", tag, rom_reason)
            return None
        try:
            margin_1lot = await executor.get_order_margin_for_structure(
                prot1, inc1, f"{tag}-SIZING"
            )
        except Exception as exc:  # noqa: BLE001 — never size a trade on a fake number if this fails
            _LOG.error("%s skipped reason=margin_fetch_failed detail=%s", tag, exc)
            return None
        if margin_1lot.total_margin <= 0.0 or margin_1lot.total_margin < max_loss_1lot:
            _LOG.error(
                "%s skipped reason=implausible_margin real=%.0f theoretical=%.0f -- refusing "
                "rather than trusting a parsing bug",
                tag,
                margin_1lot.total_margin,
                max_loss_1lot,
            )
            return None
        sizing = size_position(
            self._options_capital,
            self._risk_per_trade_pct,
            max_loss_1lot,
            margin_per_lot=margin_1lot.total_margin,
            available_margin=available_margin,
            current_greeks=self.portfolio_greeks(),
            per_lot_greeks=to_rupees(_per_lot_greeks(chain, prot1, inc1, 1), chain.spot),
            greeks_budget=self._greeks_budget,
            margin_safety_pct=self._margin_safety_pct,
        )
        if sizing.rejected or sizing.lots < 1:
            _LOG.info("%s skipped reason=sizing_rejected detail=%s", tag, sizing.reason)
            return None
        lots = min(sizing.lots, int(cfg.get("max_lots", 1)))
        final = build_weekly_condor(
            chain,
            lots,
            short_delta=float(cfg.get("short_delta", 0.15)),
            wing_points=wing,
        )
        if final is None:
            _LOG.info("%s skipped reason=final_structure_build_failed lots=%d", tag, lots)
            return None
        protection_legs, income_legs = final
        net_premium = _net_premium(protection_legs, income_legs)
        max_loss = wing * chain.lot_size * lots - net_premium
        max_profit = net_premium
        _risk_blocked = self.shared_risk_ceiling_reason(max_loss)
        if _risk_blocked is not None:
            _LOG.info("%s skipped reason=%s", tag, _risk_blocked)
            return None

        signal_id = f"SELLER-{uuid.uuid4().hex[:10].upper()}"
        crm_signal = build_seller_crm_signal(
            signal_id=signal_id,
            underlying=underlying,
            strategy_type=WEEKLY_CONDOR_TYPE,
            lots=lots,
            max_loss=max_loss,
            max_profit=max_profit,
            regime=regime,
            vix=vix if vix is not None else 15.0,
            registry_name=name,
        )
        # Own name everywhere (logs, counters, registry policy) -- B is its own strategy.
        crm_signal.strategy_name = name
        _LOG.info(
            "%s candidate expiry=%s dte=%d lots=%d credit=%.0f max_loss=%.0f legs=%s",
            tag,
            expiry,
            dte_value,
            lots,
            net_premium,
            max_loss,
            [
                f"{leg.direction[0]}{leg.strike:g}{leg.instrument_type}"
                for leg in protection_legs + income_legs
            ],
        )
        if not self._crm_precheck(crm, crm_signal, crm_ctx, lots, underlying):
            return None
        if not await self._g9_review(
            crm_signal,
            crm_ctx,
            underlying=underlying,
            strategy_type=WEEKLY_CONDOR_TYPE,
            regime=regime,
            max_loss=max_loss,
            max_profit=max_profit,
            lots=lots,
            vix=vix,
        ):
            return None
        self._stamp_open_strategies(crm_ctx)
        decision = crm.evaluate(crm_signal, crm_ctx)
        log_gate_decisions(crm_signal, decision)
        if not decision.approved or decision.final_quantity < lots:
            _LOG.info(
                "%s skipped reason=crm gate=%s detail=%s crm_qty=%s lots=%d",
                tag,
                decision.blocking_gate,
                decision.blocking_reason,
                decision.final_quantity,
                lots,
            )
            return None
        return await self._execute_and_record(
            underlying=underlying,
            strategy_type=WEEKLY_CONDOR_TYPE,
            protection_legs=protection_legs,
            income_legs=income_legs,
            lots=lots,
            chain=chain,
            net_premium=net_premium,
            max_loss=max_loss,
            max_profit=max_profit,
            direction="credit",
            signal_id=signal_id,
            regime=regime,
            today=today,
            now=now,
            is_paper=mode_paper,
        )

    async def run_expiry_day_cycle(
        self,
        underlying: str,
        *,
        today: date,
        now: datetime,
        available_margin: float,
        regime: str,
        vix: float | None,
        crm: CentralRiskManager | None,
        crm_ctx: CRMContext | None,
    ) -> OpenPosition | None:
        """Hypothesis F2 -- NIFTY expiry-day ATM iron butterfly (config options_expiry_day,
        docs/hypotheses/F_nifty_expiry_day_condor.md). Only on the expiry day itself, inside
        the entry window, once per expiry (DB-checked). Paper/live follows the strategy's own
        registry execution_mode. Shares the desk's controls (entry blocks, event blackout,
        caps, risk budget + margin sizing, G9 under its own name, CRM G0-G10, shared
        execution path) with two deliberate exceptions documented in config: the guardian
        lets it hold until exit_by, and the Greeks budget is skipped (defined-risk wings)."""
        cfg = _seller_settings.expiry_day()
        if not cfg.get("enabled", False):
            return None
        if str(cfg.get("structure", "butterfly")) == "strangle":
            return None  # the strangle is entered by the fast loop in short_premium.py, not this 5-minute cycle
        name = str(cfg.get("registry_name", EXPIRY_DAY_TYPE))
        tag = f"expiry_day underlying={underlying}"
        if crm is None or crm_ctx is None:
            _LOG.warning("%s skipped reason=crm_unavailable (fail-closed)", tag)
            return None
        mode_paper = self.strategy_is_paper(name, crm_ctx)
        executor = self._executor_for(mode_paper)
        if executor is None:
            _LOG.error(
                "%s skipped reason=mode_not_wired mode=%s -- refusing",
                tag,
                "paper" if mode_paper else "live",
            )
            return None
        start, end = (
            datetime.strptime(x, "%H:%M").time()
            for x in cfg.get("entry_window", ["10:00", "10:20"])
        )
        if not (start <= now.time() <= end):
            return None
        if any(p.strategy_type == EXPIRY_DAY_TYPE for p in self.positions.values()):
            _LOG.info("%s skipped reason=already_open", tag)
            return None
        # 2026-10-09: removed the max_strategies_per_index check, same as the condor's
        # 2026-10-07 fix -- found live 2026-10-08 (3/3 NIFTY open blocked the fly). It gets
        # one attempt per expiry and already_open above caps it at one concurrent fly, so
        # that is its own slot; CRM G7 exempts it too (_HOSTED_OPTIONS_STRATEGIES).
        blocked_reason = self.entries_blocked_reason(today, available_margin)
        if blocked_reason is not None:
            _LOG.warning("%s skipped reason=entries_blocked detail=%s", tag, blocked_reason)
            return None
        _strat_halted = self.strategy_halted_reason(name, today)
        if _strat_halted is not None:
            _LOG.warning("%s skipped reason=%s", tag, _strat_halted)
            return None
        # fetch_tier1() cannot see today's expiry on expiry day (Kite stamps it 00:00, so the
        # chain builder's `(expiry - now).days >= 0` is false) and hands back next week's --
        # the fly never traded on 2026-10-13. Ask for today's expiry explicitly; None means
        # today is not an expiry. Not fixed in the builder: the vol engine/gates rely on it.
        chain = await self._chain_fetcher.fetch_for_expiry(underlying, today)
        if chain is None:
            _LOG.info("%s skipped reason=not_expiry_day (no chain for today's expiry)", tag)
            return None
        # Deliberately not _observe_chain(): expiry-day ATM IV is inflated and would pollute the
        # desk's shared IV/day-reference state.
        expiry = chain.expiry.date()
        if self._repo is not None and await self._repo.has_position_for_expiry(
            strategy_type=EXPIRY_DAY_TYPE, underlying=underlying, expiry=expiry, is_paper=mode_paper
        ):
            _LOG.info("%s skipped reason=already_traded_this_expiry", tag)
            return None
        blackout = await self._event_calendar.selling_blocked(underlying, now)
        if blackout.blocked:
            _LOG.info("%s skipped reason=event_blackout detail=%s", tag, blackout.reason)
            return None

        wing = float(cfg.get("wing_points", 150))
        built = build_expiry_butterfly(chain, 1, wing_points=wing)
        if built is None:
            _LOG.info("%s skipped reason=structure_build_failed (strikes/liquidity)", tag)
            return None
        prot1, inc1 = built
        credit_1lot = _net_premium(prot1, inc1)
        max_loss_1lot = wing * chain.lot_size - credit_1lot
        if credit_1lot <= 0.0 or max_loss_1lot <= 0.0:
            _LOG.info(
                "%s skipped reason=degenerate_economics credit=%.0f max_loss=%.0f",
                tag,
                credit_1lot,
                max_loss_1lot,
            )
            return None
        budget = self._greeks_budget
        if cfg.get("skip_greeks_budget", True):
            # Documented strategy-only exception (config _note): defined-risk, intraday hold.
            budget = GreeksBudget(vega=1e15, delta=1e15, theta=1e15, gamma=1e15)
        try:
            margin_1lot = await executor.get_order_margin_for_structure(
                prot1, inc1, f"{tag}-SIZING"
            )
        except Exception as exc:  # noqa: BLE001 — never size a trade on a fake number if this fails
            _LOG.error("%s skipped reason=margin_fetch_failed detail=%s", tag, exc)
            return None
        if margin_1lot.total_margin <= 0.0 or margin_1lot.total_margin < max_loss_1lot:
            _LOG.error(
                "%s skipped reason=implausible_margin real=%.0f theoretical=%.0f -- refusing "
                "rather than trusting a parsing bug",
                tag,
                margin_1lot.total_margin,
                max_loss_1lot,
            )
            return None
        sizing = size_position(
            self._options_capital,
            self._risk_per_trade_pct,
            max_loss_1lot,
            margin_per_lot=margin_1lot.total_margin,
            available_margin=available_margin,
            current_greeks=self.portfolio_greeks(),
            per_lot_greeks=to_rupees(_per_lot_greeks(chain, prot1, inc1, 1), chain.spot),
            greeks_budget=budget,
            margin_safety_pct=self._margin_safety_pct,
        )
        if sizing.rejected or sizing.lots < 1:
            _LOG.info("%s skipped reason=sizing_rejected detail=%s", tag, sizing.reason)
            return None
        lots = min(sizing.lots, int(cfg.get("max_lots", 1)))
        final = build_expiry_butterfly(chain, lots, wing_points=wing)
        if final is None:
            _LOG.info("%s skipped reason=final_structure_build_failed lots=%d", tag, lots)
            return None
        protection_legs, income_legs = final
        net_premium = _net_premium(protection_legs, income_legs)
        max_loss = wing * chain.lot_size * lots - net_premium
        # No shared_risk_ceiling check: the fly has its own slot (one try per expiry, wings
        # cap its loss, sized by the risk budget, closed by exit_by) and must not be starved
        # by the desk's spreads -- found live 2026-10-13 (open Rs57,144 + fly Rs5,596 > Rs60,000).
        qty = chain.lot_size * lots
        model_pts = model_credit_points(protection_legs, income_legs, chain.spot, vix, now)
        real_pts = net_premium / qty if qty else 0.0
        _LOG.info(
            "%s candidate expiry=%s mode=%s lots=%d atm=%g wing=%g real_credit_pts=%.1f "
            "model_credit_pts=%s real_vs_model=%s max_loss=%.0f vix=%s",
            tag,
            expiry,
            "paper" if mode_paper else "live",
            lots,
            income_legs[0].strike,
            wing,
            real_pts,
            f"{model_pts:.1f}" if model_pts is not None else "NA",
            f"{real_pts / model_pts:.2f}" if model_pts else "NA",
            max_loss,
            vix,
        )
        signal_id = f"SELLER-{uuid.uuid4().hex[:10].upper()}"
        crm_signal = build_seller_crm_signal(
            signal_id=signal_id,
            underlying=underlying,
            strategy_type=EXPIRY_DAY_TYPE,
            lots=lots,
            max_loss=max_loss,
            max_profit=net_premium,
            regime=regime,
            vix=vix if vix is not None else 15.0,
            registry_name=name,
        )
        crm_signal.strategy_name = name
        if not self._crm_precheck(crm, crm_signal, crm_ctx, lots, underlying):
            return None
        if not await self._g9_review(
            crm_signal,
            crm_ctx,
            underlying=underlying,
            strategy_type=EXPIRY_DAY_TYPE,
            regime=regime,
            max_loss=max_loss,
            max_profit=net_premium,
            lots=lots,
            vix=vix,
        ):
            return None
        self._stamp_open_strategies(crm_ctx)
        decision = crm.evaluate(crm_signal, crm_ctx)
        log_gate_decisions(crm_signal, decision)
        if not decision.approved or decision.final_quantity < lots:
            _LOG.info(
                "%s skipped reason=crm gate=%s detail=%s",
                tag,
                decision.blocking_gate,
                decision.blocking_reason,
            )
            return None
        return await self._execute_and_record(
            underlying=underlying,
            strategy_type=EXPIRY_DAY_TYPE,
            protection_legs=protection_legs,
            income_legs=income_legs,
            lots=lots,
            chain=chain,
            net_premium=net_premium,
            max_loss=max_loss,
            max_profit=net_premium,
            direction="credit",
            signal_id=signal_id,
            regime=regime,
            today=today,
            now=now,
            is_paper=mode_paper,
            skip_shared_risk=True,
        )

    async def sweep_expiry_guardian(self, today: date, now: datetime) -> None:
        """LAYER0-4 forced closes (options_spec.md §6) — highest priority,
        called before any other sweep in the orchestrator loop ordering."""
        for position_id, pos in list(self.positions.items()):
            d = compute_dte(pos.expiry, today)
            _strangle = is_strangle_position(pos, EXPIRY_DAY_TYPE)
            if pos.strategy_type == EXPIRY_DAY_TYPE and pos.expiry == today and not _strangle:
                # Strategy-only exception (config options_expiry_day): it exists to hold
                # on expiry day, so the guardian takes over only from exit_by onward.
                exit_by = datetime.strptime(
                    str(_seller_settings.expiry_day().get("exit_by", "14:45")), "%H:%M"
                ).time()
                if now.time() < exit_by:
                    continue
            if pos.strategy_type == SHORT_PREMIUM_TYPE or _strangle:
                # Intraday by design: the strategy's own loop exits at its exit_time; the guardian
                # only takes over from there as the backstop (a stale prior-day row falls through).
                exit_str = str(pos.meta.get("exit_time") or "15:01")
                if (
                    pos.entered_at.astimezone(_IST).date() == today
                    and now.time() < datetime.strptime(exit_str, "%H:%M").time()
                ):
                    continue
            action = guardian_evaluate(d, now.time(), is_expiry_day=(pos.expiry == today))
            if action.close_now:
                await self._close_position(position_id, reason=action.reason)

    async def sweep_position_manager(
        self,
        *,
        today: date,
        blackout_active: bool,
        current_prices: dict[str, float],
        current_ivs: dict[str, float],
        chains: dict[str, OptionChain] | None = None,
    ) -> None:
        """Profit/stop/delta-breach/IV-crush checks (options_spec.md §10), plus
        the §10 adjustment engine (roll the untested income leg) when a delta
        breach fires and ``adjustments.execute_rolls`` is on
        (config/options_settings.json — off by default; when off, a breach is
        only logged, matching every other behavior exactly as before this
        method could execute a roll).

        ``chains`` maps ``pos.underlying`` (e.g. "NSE:NIFTY") to that
        underlying's current OptionChain — required to find a new strike and
        its bid/ask when a roll executes; a position whose underlying has no
        chain this cycle just skips the roll attempt (logged), not a crash.

        Entry/current IV comparison is not wired (0.0/0.0 → IV-crush check
        never fires) — this build doesn't persist per-position entry IV
        anywhere yet (see module docstring on in-memory state); documented
        gap, not a silent no-op presented as complete.
        """
        chains = chains or {}
        # Operator-requested closes first (config `operator_actions.close_position_ids`, read
        # fresh each sweep). Audited, one-shot per id: once closed it is no longer in
        # self.positions, so a leftover id in the file is inert.
        for _op_id in _seller_settings.operator_close_position_ids():
            if _op_id in self.positions:
                _LOG.warning("seller_desk_operator_close id=%s reason=OPERATOR_CLOSE", _op_id)
                await self._close_position(_op_id, reason="OPERATOR_CLOSE")
        for _u, _c in chains.items():
            self._observe_chain(_u, _c, today)
        expiry_chains: dict[tuple[str, date], OptionChain | None] = {}
        for position_id, pos in list(self.positions.items()):
            unrealized = self._unrealized_pnl(pos, current_prices)
            missing = [
                leg.tradingsymbol
                for leg in pos.protection_legs + pos.income_legs
                if leg.tradingsymbol not in current_prices
            ]
            if missing:
                # _unrealized_pnl falls back to the ENTRY premium for an unpriced leg, i.e.
                # that leg's move reads as zero -- stop/target are blind to it. Say so.
                _LOG.warning(
                    "seller_desk_pnl_price_missing id=%s legs=%s -- P&L uses entry premium "
                    "for these legs, stop/target cannot see their move",
                    position_id,
                    missing,
                )
            elif pos.peak_unrealized_pnl is None or unrealized > pos.peak_unrealized_pnl:
                pos.peak_unrealized_pnl = unrealized
                if unrealized > 0.0 and self._repo is not None:
                    try:
                        await self._repo.update_peak_unrealized(position_id, unrealized)
                    except Exception as exc:  # noqa: BLE001 — best-effort, in-memory value still holds
                        _LOG.warning(
                            "seller_desk_peak_persist_failed id=%s err=%s", position_id, exc
                        )
            if pos.strategy_type == SHORT_PREMIUM_TYPE or is_strangle_position(
                pos, EXPIRY_DAY_TYPE
            ):
                # Per-leg SL + time exit live in src/options/short_premium.py (own 10s loop; this
                # 5-minute sweep is far too coarse for a 23% leg stop). No rolls, ever.
                continue
            if pos.strategy_type == WEEKLY_CONDOR_TYPE:
                # Own TP/SL, never rolled (hypothesis B). Expiry guardian still applies.
                await self._manage_weekly_condor(position_id, pos, unrealized, today, missing)
                continue
            if pos.strategy_type == EXPIRY_DAY_TYPE:
                await self._manage_expiry_day(position_id, pos, unrealized, today, missing)
                continue
            # OptionLeg.delta is set once at entry and never touched again --
            # without this refresh, check_delta_breach_any_leg keeps comparing
            # against the entry-time delta forever, so a breach can never fire
            # no matter how far the underlying has actually moved (found
            # 2026-09-16: a 10-day-old NIFTY bull_put_spread whose short strike
            # went from -0.26 delta to near-ATM never logged a single breach).
            # The chain must be the position's OWN expiry: `chains` holds only the nearest
            # expiry, and a strike-only match against it reads another expiry's delta
            # (found 2026-09-28: an Oct 13 spread refreshed from the Sep 29 chain).
            leg_chain = await self._chain_for_position(pos, chains, expiry_chains)
            if leg_chain is None:
                _LOG.warning(
                    "seller_desk_no_chain_for_position id=%s underlying=%s expiry=%s -- leg "
                    "deltas not refreshed, rolls unavailable this cycle",
                    position_id,
                    pos.underlying,
                    pos.expiry,
                )
            else:
                by_strike = {row.strike: row for row in leg_chain.strikes}
                for leg in pos.income_legs:
                    row = by_strike.get(leg.strike)
                    current_delta = (
                        (row.call_delta if leg.instrument_type == "CE" else row.put_delta)
                        if row is not None
                        else None
                    )
                    if current_delta is not None:
                        leg.delta = current_delta
                    else:
                        _LOG.warning(
                            "seller_desk_leg_delta_stale id=%s leg=%s -- no live delta in the chain, "
                            "breach check is using the ENTRY delta %.2f",
                            position_id,
                            leg.tradingsymbol,
                            leg.delta,
                        )
            # Delta breach protects a CREDIT structure's short strike. In a debit spread the
            # short leg is the hedge and its delta rising means the spread is winning; the
            # risk there is the long leg decaying, which the debit stop / breakeven lock
            # cover. Applying the credit rule would close a winner as a "breach".
            breach = (
                check_delta_breach_any_leg(
                    pos.income_legs,
                    blackout_active=blackout_active,
                    # a bull_put/bear_call vertical (one short leg, nothing to roll) has its own
                    # threshold; keyed on the structure, NOT leg count -- a condor reduced to one
                    # leg by a per-leg close is naked-ish and keeps the condor rule.
                    threshold=(
                        single_sided_tested_delta(blackout_active=blackout_active)
                        if pos.strategy_type in _SINGLE_SIDED_TYPES and len(pos.income_legs) == 1
                        else None
                    ),
                )
                if pos.direction == "credit"
                else ManagementAction("NONE", "delta breach not applicable to debit")
            )
            if breach.action == "ADJUST" and not pos.was_breached:
                pos.was_breached = True
                if self._repo is not None:
                    try:
                        await self._repo.mark_breached(position_id)
                    except Exception as exc:  # noqa: BLE001 — best-effort flag
                        _LOG.warning(
                            "seller_desk_mark_breached_failed id=%s err=%s", position_id, exc
                        )
            actions = pm_evaluate_position(
                direction=pos.direction,
                short_strike_delta=None,  # breach already checked above (multi-leg)
                unrealized_pnl=unrealized,
                # Max profit of a credit spread is the credit received. entry_margin is the
                # max LOSS, so a 50% target of it exceeded the credit and never fired.
                max_potential=abs(pos.entry_premium),
                premium=abs(pos.entry_premium),
                dte=compute_dte(pos.expiry, today),
                entry_iv=0.0,
                current_iv=0.0,
                blackout_active=blackout_active,
                peak_unrealized_pnl=pos.peak_unrealized_pnl,
            )
            # One line per open position per sweep: silence used to be indistinguishable
            # from "checked, nothing to do" (four "no adjustments" reports, 2026-09).
            _premium = abs(pos.entry_premium)
            _LOG.info(
                "seller_desk_position_check id=%s underlying=%s type=%s dir=%s dte=%d "
                "unrealized=%.0f pct_of_premium=%.0f peak=%s chain_expiry=%s breach=%s "
                "triggered=%s legs=%s",
                position_id,
                pos.underlying,
                pos.strategy_type,
                pos.direction,
                compute_dte(pos.expiry, today),
                unrealized,
                (unrealized / _premium * 100.0) if _premium > 0.0 else 0.0,
                f"{pos.peak_unrealized_pnl:.0f}" if pos.peak_unrealized_pnl is not None else None,
                leg_chain.expiry.date() if leg_chain is not None else None,
                breach.action,
                [a.reason for a in actions if a.action != "NONE"] or None,
                [
                    f"{leg.tradingsymbol}:{leg.direction[0]}:{leg.premium:g}->"
                    f"{current_prices.get(leg.tradingsymbol, 'NA')}(d={leg.delta:.2f})"
                    for leg in pos.protection_legs + pos.income_legs
                ],
            )
            if any(a.action == "CLOSE" for a in actions):
                reasons = ", ".join(a.reason for a in actions if a.action == "CLOSE")
                await self._close_position(position_id, reason=reasons)
                continue
            if breach.action != "ADJUST":
                continue
            _LOG.warning(
                "seller_desk_delta_breach id=%s underlying=%s side=%s reason=%s",
                position_id,
                pos.underlying,
                breach.breached_instrument_type,
                breach.reason,
            )
            if _seller_settings.adjustment_settings().get("execute_rolls", False):
                await self._attempt_adjustment(
                    position_id, pos, breach, leg_chain, today, unrealized
                )

    async def _manage_weekly_condor(
        self,
        position_id: str,
        pos: OpenPosition,
        unrealized: float,
        today: date,
        missing_legs: list[str],
    ) -> None:
        """Hypothesis B exits: close at profit_target_pct of the credit, or when the
        loss reaches stop_loss_credit_multiple x credit. No decision on a cycle with
        an unpriced leg (its move would read as zero) -- the guardian still forces
        the pre-expiry exit regardless."""
        cfg = _seller_settings.weekly_condor()
        credit = abs(pos.entry_premium)
        tp = float(cfg.get("profit_target_pct", 50)) / 100.0 * credit
        sl = float(cfg.get("stop_loss_credit_multiple", 2.0)) * credit
        reason: str | None = None
        if not missing_legs and credit > 0.0:
            if unrealized >= tp:
                reason = f"WEEKLY_CONDOR_TP pnl={unrealized:.0f} >= {tp:.0f}"
            elif unrealized <= -sl:
                reason = f"WEEKLY_CONDOR_SL pnl={unrealized:.0f} <= -{sl:.0f}"
        _LOG.info(
            "seller_desk_position_check id=%s underlying=%s type=%s dir=%s dte=%d "
            "unrealized=%.0f pct_of_premium=%.0f tp=%.0f sl=-%.0f missing_legs=%s triggered=%s",
            position_id,
            pos.underlying,
            pos.strategy_type,
            pos.direction,
            compute_dte(pos.expiry, today),
            unrealized,
            (unrealized / credit * 100.0) if credit > 0.0 else 0.0,
            tp,
            sl,
            missing_legs or None,
            reason,
        )
        if reason is not None:
            await self._close_position(position_id, reason=reason)

    async def _manage_expiry_day(
        self,
        position_id: str,
        pos: OpenPosition,
        unrealized: float,
        today: date,
        missing_legs: list[str],
    ) -> None:
        """Hypothesis F2 exits: TP at profit_target_pct of the credit, SL when the loss
        reaches stop_loss_credit_multiple x credit, and a hard time exit at exit_by (the
        guardian also closes from exit_by). TP/SL skip a cycle with an unpriced leg; the
        time exit never does."""
        cfg = _seller_settings.expiry_day()
        credit = abs(pos.entry_premium)
        tp = float(cfg.get("profit_target_pct", 50)) / 100.0 * credit
        sl = float(cfg.get("stop_loss_credit_multiple", 2.0)) * credit
        exit_by = datetime.strptime(str(cfg.get("exit_by", "14:45")), "%H:%M").time()
        now_t = datetime.now(_IST).time()
        reason: str | None = None
        if pos.expiry <= today and now_t >= exit_by:
            reason = f"EXPIRY_DAY_TIME_EXIT {exit_by:%H:%M}"
        elif not missing_legs and credit > 0.0:
            if unrealized >= tp:
                reason = f"EXPIRY_DAY_TP pnl={unrealized:.0f} >= {tp:.0f}"
            elif unrealized <= -sl:
                reason = f"EXPIRY_DAY_SL pnl={unrealized:.0f} <= -{sl:.0f}"
        _LOG.info(
            "seller_desk_position_check id=%s underlying=%s type=%s mode=%s unrealized=%.0f "
            "pct_of_premium=%.0f tp=%.0f sl=-%.0f missing_legs=%s triggered=%s",
            position_id,
            pos.underlying,
            pos.strategy_type,
            "paper" if self._pos_is_paper(pos) else "live",
            unrealized,
            (unrealized / credit * 100.0) if credit > 0.0 else 0.0,
            tp,
            sl,
            missing_legs or None,
            reason,
        )
        if reason is not None:
            await self._close_position(position_id, reason=reason)

    def _same_direction_open(self, strategy_type: str) -> list[str]:
        """Ids of open positions on the same directional side as ``strategy_type`` when that
        already meets ``position_limits.max_same_direction_strategies`` (default 1; 0 turns the
        guard off); empty when the candidate may proceed. Non-directional structures
        (condors, butterflies, straddles) are never blocked here."""
        side = _DIRECTIONAL_SIDE.get(strategy_type)
        limit = _seller_settings.max_same_direction_strategies()
        if side is None or limit <= 0:
            return []
        open_ids = [
            pid
            for pid, p in self.positions.items()
            if _DIRECTIONAL_SIDE.get(p.strategy_type) == side
        ]
        return open_ids if len(open_ids) >= limit else []

    async def _chain_for_position(
        self,
        pos: OpenPosition,
        chains: dict[str, OptionChain],
        cache: dict[tuple[str, date], OptionChain | None],
    ) -> OptionChain | None:
        """The chain for ``pos``'s own expiry: the already-fetched nearest-expiry chain if
        that is the same expiry, else one fetch per (underlying, expiry) per sweep. ``None``
        when unavailable -- never a chain of a different expiry."""
        near = chains.get(pos.underlying)
        if near is not None:
            near_expiry = near.expiry.date() if isinstance(near.expiry, datetime) else near.expiry
            if near_expiry == pos.expiry:
                return near
        key = (pos.underlying, pos.expiry)
        if key not in cache:
            try:
                cache[key] = await self._chain_fetcher.fetch_for_expiry(pos.underlying, pos.expiry)
            except Exception as exc:  # noqa: BLE001 — one failed fetch must not stop the sweep
                _LOG.warning(
                    "seller_desk_expiry_chain_fetch_failed underlying=%s expiry=%s err=%s",
                    pos.underlying,
                    pos.expiry,
                    exc,
                )
                cache[key] = None
        return cache[key]

    async def _attempt_adjustment(
        self,
        position_id: str,
        pos: OpenPosition,
        breach: ManagementAction,
        chain: OptionChain | None,
        today: date,
        unrealized_pnl: float,
    ) -> None:
        """Executes options_spec.md §10's adjustment: cost-benefit gate first
        (adjustment_engine.evaluate_adjustment), then roll the untested
        income leg toward price (find_strike_by_delta at the structure's own
        original target delta) or close the position if the gate says the
        cure is worse than the disease. Only reachable when
        adjustments.execute_rolls is on -- see sweep_position_manager.

        Single-sided credit verticals (bull_put_spread/bear_call_spread) have
        only one income leg -- there is no untested side to roll, and unlike
        an Iron Condor a roll here has no safe-side premium funding it (it
        just re-underwrites the same directional bet at a worse cost basis).
        The spec's roll mechanism is Iron-Condor-specific (§10 example);
        for a single-leg structure a delta breach is treated as a close
        signal instead of silently skipping (found 2026-09-16 -- a breached
        bull_put_spread logged the breach every cycle but never acted)."""
        if len(pos.income_legs) < 2:
            await self._close_position(
                position_id, reason=f"DELTA_BREACH_NO_UNTESTED_SIDE {breach.reason}"
            )
            return
        untested_leg = next(
            (
                leg
                for leg in pos.income_legs
                if leg.instrument_type != breach.breached_instrument_type
            ),
            None,
        )
        if untested_leg is None or chain is None:
            _LOG.warning(
                "seller_desk_adjustment_skipped id=%s reason=%s",
                position_id,
                "no_untested_leg" if untested_leg is None else "no_chain_for_underlying",
            )
            return

        new_row = find_strike_by_delta(chain, target_short_delta(), untested_leg.instrument_type)
        if new_row is None or new_row.strike == untested_leg.strike:
            _LOG.warning(
                "seller_desk_adjustment_skipped id=%s reason=no_candidate_strike", position_id
            )
            return
        new_leg = leg_from_chain_row(
            new_row, untested_leg.instrument_type, "SELL", untested_leg.lots, untested_leg.lot_size
        )
        old_row = next((r for r in chain.strikes if r.strike == untested_leg.strike), None)
        old_quote = (
            (old_row.call if untested_leg.instrument_type == "CE" else old_row.put)
            if old_row is not None
            else None
        )
        new_quote = new_row.call if untested_leg.instrument_type == "CE" else new_row.put
        if new_leg is None or old_quote is None or new_quote is None:
            _LOG.warning("seller_desk_adjustment_skipped id=%s reason=no_quote", position_id)
            return

        # Cost-benefit uses the 2 legs actually touched (1 close + 1 open) --
        # the spec's "4 legs" line reads like boilerplate for a full-structure
        # rebuild and contradicts its own single-side worked example.
        notional = untested_leg.strike * untested_leg.quantity
        cost = estimate_adjustment_cost(
            [(old_quote.bid, old_quote.ask), (new_quote.bid, new_quote.ask)], notional
        )
        # Spec: "current_credit_value - already_captured_profit". This build has
        # no separate mark-to-market credit-value feed, so already-realized
        # gain is used as the proxy -- documented approximation, not spec-exact.
        remaining_premium = max(0.0, abs(pos.entry_premium) - max(0.0, unrealized_pnl))

        decision = evaluate_adjustment(
            dte=compute_dte(pos.expiry, today),
            adjustments_so_far=pos.adjustments_made,
            adjustment_cost=cost,
            remaining_premium=remaining_premium,
        )
        if decision.action == "CLOSE":
            await self._close_position(position_id, reason=decision.reason)
            return

        rolled = await self._executor.roll_leg(untested_leg, new_leg, position_id)
        if rolled is None:
            _LOG.warning("seller_desk_adjustment_roll_failed id=%s", position_id)
            return
        pos.income_legs = [rolled if leg is untested_leg else leg for leg in pos.income_legs]
        pos.adjustments_made += 1
        _LOG.warning(
            "seller_desk_adjustment_executed id=%s old_strike=%.0f new_strike=%.0f "
            "adjustments_made=%d",
            position_id,
            untested_leg.strike,
            rolled.strike,
            pos.adjustments_made,
        )
        if self._repo is not None:
            try:
                await self._repo.record_adjustment(
                    position_id,
                    protection_legs=pos.protection_legs,
                    income_legs=pos.income_legs,
                    old_leg=untested_leg,
                    new_leg=rolled,
                    adjustment_cost=cost,
                    remaining_premium=remaining_premium,
                    reason=decision.reason,
                    adjustments_made=pos.adjustments_made,
                    adjusted_at=datetime.now(_IST),
                )
            except Exception as exc:  # noqa: BLE001 — persistence is best-effort, never blocks a live roll
                _LOG.warning("seller_desk_adjustment_persist_failed id=%s err=%s", position_id, exc)

    async def sweep_margin_monitor(self, current_margins: dict[str, float]) -> None:
        """Margin-shock detection (options_spec.md §11)."""
        for position_id, pos in list(self.positions.items()):
            current = current_margins.get(position_id)
            if current is None:
                continue
            alert = evaluate_position_margin(pos.entry_margin, current)
            if alert.level == "CRITICAL":
                await self._close_position(position_id, reason=alert.reason)

    async def _run_autopsy(self, pos: OpenPosition, realized_pnl: float | None) -> None:
        """Post-close autopsy (options_spec.md 18): run every question we have inputs for
        and store the findings in options_learning_log. Best-effort -- never blocks or
        fails a close. Counterfactual questions (hold-to-expiry, no-adjustment) are
        unknowable at close time and are skipped, not guessed."""
        try:
            last_regime = self._last_regime.get(pos.underlying)
            spot_now = self._spot.get(pos.underlying)
            iv_now = self._atm_iv.get(pos.underlying)
            iv_change_pct = (
                (iv_now - pos.entry_atm_iv) / pos.entry_atm_iv * 100.0
                if iv_now is not None and pos.entry_atm_iv
                else None
            )
            attribution = None
            if spot_now is not None and pos.entry_spot and iv_now is not None and pos.entry_atm_iv:
                held_days = (datetime.now(_IST) - pos.entered_at).total_seconds() / 86400.0
                attribution = pnl_attribution_native(
                    self._native_greeks(pos),
                    spot_move=spot_now - pos.entry_spot,
                    iv_change_points=(iv_now - pos.entry_atm_iv) * 100.0,
                    days=max(held_days, 0.0),
                )
            pnl = realized_pnl if realized_pnl is not None else 0.0
            findings = run_autopsy(
                regime_at_entry=pos.entry_regime,
                regime_turned_trending=(
                    last_regime.endswith("TRENDING") if last_regime is not None else None
                ),
                target_delta=pos.entry_short_delta,
                short_strike_was_breached=pos.was_breached,
                sold_expecting_iv_drop=pos.direction == "credit",
                iv_change_pct=iv_change_pct,
                closed_at_pct_of_max=(
                    pnl / pos.entry_premium * 100.0
                    if realized_pnl is not None
                    and pos.direction == "credit"
                    and pos.entry_premium > 0
                    else None
                ),
                would_have_captured_holding_to_expiry_pct=None,
                adjustments_made=pos.adjustments_made,
                pnl_with_adjustments=pnl,
                estimated_pnl_without_adjustment=None,
                delta_pnl=attribution.delta_pnl if attribution else None,
                gamma_pnl=attribution.gamma_pnl if attribution else None,
                theta_pnl=attribution.theta_pnl if attribution else None,
                vega_pnl=attribution.vega_pnl if attribution else None,
            )
            for f in findings:
                _LOG.info(
                    "seller_desk_autopsy id=%s q=%s finding=%s",
                    pos.position_id,
                    f.question,
                    f.finding,
                )
                if self._repo is not None:
                    await self._repo.insert_learning_finding(
                        pos.position_id, f.question, f.finding, f.confidence
                    )
        except Exception as exc:  # noqa: BLE001 — autopsy must never affect a close
            _LOG.warning("seller_desk_autopsy_failed id=%s err=%s", pos.position_id, exc)

    def _log_greeks_advisories(self) -> None:
        """Portfolio Greeks vs the weekly budgets (options_spec.md 8): advisories at 80% of
        budget, breach above it. Advisory only -- reducing a position is position_manager's
        job; auto-hedging via futures does not exist (no futures order path)."""
        if not self.positions:
            return
        rs = self.portfolio_greeks()
        portfolio = PortfolioGreeksRs(rs.net_delta, rs.net_gamma, rs.net_theta, rs.net_vega)
        legs = []
        for pos in self.positions.values():
            p = to_rupees(self._native_greeks(pos), self._spot_for(pos))
            legs.append(
                LegGreekInputs(
                    pos.position_id, p.net_delta, p.net_gamma, p.net_theta, p.net_vega, 1, 1
                )
            )
        budget = self._greeks_budget
        for adv in (
            check_delta_budget(portfolio, budget.delta),
            check_vega_budget(portfolio, budget.vega, legs),
            check_gamma_budget(portfolio, budget.gamma, legs),
        ):
            if adv.level != "OK":
                _LOG.warning(
                    "seller_desk_greeks_%s dim=%s %s contributor=%s",
                    adv.level.lower(),
                    adv.dimension,
                    adv.message,
                    adv.largest_contributor_position_id,
                )

    async def _maybe_record_attribution(self, today: date, now: datetime | None) -> None:
        """Once per day after 15:20 IST: write the day's Greeks P&L attribution
        (options_pnl_attribution, spec section 8). Uses entry-time Greeks x the day's spot /
        ATM-IV change since first observation; a documented approximation (Greeks are not
        refreshed intraday)."""
        if self._repo is None or now is None or self._attribution_day == today:
            return
        if (now.hour, now.minute) < (15, 20) or not self.positions:
            return
        totals = [0.0, 0.0, 0.0, 0.0]  # delta, gamma, theta, vega pnl
        native_sum = PortfolioGreeks()
        for underlying in {p.underlying for p in self.positions.values()}:
            ref = self._day_ref.get(underlying)
            spot_now = self._spot.get(underlying)
            iv_now = self._atm_iv.get(underlying)
            if ref is None or ref[0] != today or spot_now is None:
                continue
            _, spot0, iv0 = ref
            native = PortfolioGreeks()
            for p in (p for p in self.positions.values() if p.underlying == underlying):
                n = self._native_greeks(p)
                native.net_delta += n.net_delta
                native.net_gamma += n.net_gamma
                native.net_theta += n.net_theta
                native.net_vega += n.net_vega
            a = pnl_attribution_native(
                native,
                spot_move=spot_now - spot0,
                iv_change_points=((iv_now - iv0) * 100.0) if iv_now is not None and iv0 else 0.0,
                days=1.0,
            )
            for i, v in enumerate((a.delta_pnl, a.gamma_pnl, a.theta_pnl, a.vega_pnl)):
                totals[i] += v
            native_sum.net_delta += native.net_delta
            native_sum.net_gamma += native.net_gamma
            native_sum.net_theta += native.net_theta
            native_sum.net_vega += native.net_vega
        try:
            await self._repo.upsert_pnl_attribution(
                today,
                net_delta=native_sum.net_delta,
                net_gamma=native_sum.net_gamma,
                net_theta=native_sum.net_theta,
                net_vega=native_sum.net_vega,
                delta_pnl=totals[0],
                gamma_pnl=totals[1],
                theta_pnl=totals[2],
                vega_pnl=totals[3],
            )
            self._attribution_day = today
            _LOG.info(
                "seller_desk_pnl_attribution day=%s delta=%.0f gamma=%.0f theta=%.0f vega=%.0f",
                today,
                *totals,
            )
        except Exception as exc:  # noqa: BLE001 — reporting only
            _LOG.warning("seller_desk_attribution_persist_failed err=%s", exc)

    def _add_realized(self, pnl: float, structure_type: str | None = None) -> None:
        today = datetime.now(_IST).date()
        if self._realized_day != today:
            self._realized_day, self._realized_today = today, 0.0
            self._realized_today_by_strategy = {}
        self._realized_today += pnl
        if structure_type is not None:
            name = _registry_strategy_for(structure_type)
            self._realized_today_by_strategy[name] = (
                self._realized_today_by_strategy.get(name, 0.0) + pnl
            )

    async def _seed_strategy_daily_loss(self, today: date) -> None:
        """Restore each registry strategy's today-so-far REALIZED loss from the DB
        (self._repo.realized_pnl_by_structure_closed_on), and halt any strategy that's
        already past its per-strategy daily-loss cap on realized alone -- so a restart
        can never hand a killed strategy a fresh chance just because the in-memory
        latch was forgotten. Open-position unrealized loss is picked up by the very
        next sweep_strategy_daily_loss() cycle, same limitation _realized_today itself
        already has on restore."""
        if self._repo is None:
            return
        by_structure = await self._repo.realized_pnl_by_structure_closed_on(
            today, is_paper=self._is_paper
        )
        by_strategy: dict[str, float] = {}
        for structure_type, pnl in by_structure.items():
            name = _registry_strategy_for(structure_type)
            by_strategy[name] = by_strategy.get(name, 0.0) + pnl
        self._realized_today_by_strategy = by_strategy
        from src.risk.budget import get_budget as _rb_get_budget
        from src.risk.budget import get_current_equity as _rb_get_equity

        cap = _rb_get_budget().options.max_loss_per_day_per_strategy_rupees(_rb_get_equity())
        for name, pnl in by_strategy.items():
            if cap > 0 and -pnl >= cap:
                self._strategy_halted_day[name] = today
                _LOG.error(
                    "seller_desk_strategy_daily_loss_halt_restored strategy=%s "
                    "realized=%.0f cap=%.0f",
                    name,
                    pnl,
                    cap,
                )

    def shared_risk_ceiling_reason(self, candidate_max_loss: float) -> str | None:
        """config/capital.json options.max_share_of_shared_risk (Layer 2, "if every
        open options structure hits its own stop, what's the worst case"): the sum of
        every open structure's own defined max loss, plus this candidate, must fit
        under the ceiling. entry_margin IS each open position's max_loss (set at
        entry), so this needs no extra bookkeeping. Returns a block reason, or None."""
        from src.risk.budget import get_budget as _rb_get_budget
        from src.risk.budget import get_current_equity as _rb_get_equity

        ceiling = _rb_get_budget().options.max_share_of_shared_risk_rupees(_rb_get_equity())
        if ceiling <= 0:
            return None
        # options_intraday_short_premium and the expiry-day fly are exempt from every total/slot
        # cap and must not eat the headroom of the desk's other structures either: their risk is
        # their own defined loss.
        open_total = sum(
            p.entry_margin
            for p in self.positions.values()
            if p.strategy_type not in (SHORT_PREMIUM_TYPE, EXPIRY_DAY_TYPE)
        )
        projected = open_total + candidate_max_loss
        if projected > ceiling:
            return (
                f"shared_risk_ceiling open=Rs{open_total:.0f} candidate=Rs"
                f"{candidate_max_loss:.0f} projected=Rs{projected:.0f} > ceiling=Rs{ceiling:.0f}"
            )
        return None

    def strategy_halted_reason(self, name: str, today: date) -> str | None:
        """Why `name` (a registry strategy) may NOT open a new position right now, or
        None. Mirrors entries_blocked_reason's segment-wide version, scoped to one
        strategy; checked by each hosted/desk entry path before building a candidate."""
        if self._strategy_halted_day.get(name) == today:
            return "strategy_daily_loss_cap"
        return None

    async def sweep_strategy_daily_loss(
        self, today: date, current_prices: dict[str, float]
    ) -> None:
        """config/capital.json options.max_loss_per_day_per_strategy (2026-10-07): when
        ONE registry strategy's realized + open MTM loss today reaches its own cap,
        close THAT strategy's open positions only and latch it off until the next
        trading day -- every other strategy keeps trading. Called once per orchestrator
        sweep, same cadence as sweep_portfolio_risk (the segment-wide backstop)."""
        from src.risk.budget import get_budget as _rb_get_budget
        from src.risk.budget import get_current_equity as _rb_get_equity

        cap = _rb_get_budget().options.max_loss_per_day_per_strategy_rupees(_rb_get_equity())
        if cap <= 0:
            return
        by_strategy: dict[str, list[OpenPosition]] = {}
        for pos in self.positions.values():
            by_strategy.setdefault(_registry_strategy_for(pos.strategy_type), []).append(pos)
        for name, positions in by_strategy.items():
            if self._strategy_halted_day.get(name) == today:
                continue
            unrealized = sum(self._unrealized_pnl(p, current_prices) for p in positions)
            loss_today = self._realized_today_by_strategy.get(name, 0.0) + unrealized
            if -loss_today < cap:
                continue
            self._strategy_halted_day[name] = today
            _LOG.error(
                "seller_desk_strategy_daily_loss_halt strategy=%s realized=%.0f "
                "unrealized=%.0f cap=%.0f positions=%d",
                name,
                self._realized_today_by_strategy.get(name, 0.0),
                unrealized,
                cap,
                len(positions),
            )
            for pos in positions:
                await self._close_position(pos.position_id, reason="STRATEGY_DAILY_LOSS")

    def entries_blocked_reason(self, today: date, available_margin: float) -> str | None:
        """Why NEW entries are blocked right now, or None. Existing positions are
        never touched by these blocks -- only new risk is refused.

        - daily P&L breaker (options_spec.md 11.5): latched for the day by
          sweep_portfolio_risk().
        - margin cushion (options_spec.md 11): hysteresis block below
          ``margin.margin_cushion_minimum_pct``.
        - gap stress test: only when ``portfolio_risk.gap_risk_enforce`` is true.
        """
        if self._entries_halted_day == today:
            _LOG.debug("entries_blocked_reason_found reason=daily_pnl_circuit_breaker")
            return "daily_pnl_circuit_breaker"
        committed = sum(p.entry_margin for p in self.positions.values())
        block_pct = float(_seller_settings.margin_settings().get("margin_cushion_minimum_pct", 20))
        self._margin_blocked, margin_reason = new_entries_blocked(
            available_margin,
            committed,
            currently_blocked=self._margin_blocked,
            block_cushion_pct=block_pct,
        )
        _LOG.debug(
            "entries_blocked_reason_received available_margin=%.0f committed=%.0f "
            "block_cushion_pct=%.1f margin_blocked=%s gap_blocked=%s",
            available_margin,
            committed,
            block_pct,
            self._margin_blocked,
            self._gap_blocked,
        )
        if self._margin_blocked:
            _LOG.debug(
                "entries_blocked_reason_found reason=margin_cushion detail=%s", margin_reason
            )
            return f"margin_cushion ({margin_reason})"
        if self._gap_blocked:
            _LOG.debug("entries_blocked_reason_found reason=gap_risk_stress_test")
            return "gap_risk_stress_test"
        return None

    async def sweep_portfolio_risk(
        self,
        *,
        today: date,
        current_prices: dict[str, float],
        chains: dict[str, OptionChain],
        now: datetime | None = None,
    ) -> None:
        """Portfolio-level safety sweep (options_spec.md 11.5), run once per
        orchestrator loop after the per-position sweeps.

        1. Daily P&L breaker: today's realized + open mark-to-market loss vs
           ``portfolio_risk.daily_loss_halt_pct`` of options_capital. On breach,
           NEW entries are latched off until the next trading day.
        2. Gap stress test on the open book (entry-time Greeks x lots): the
           worst 5%/10% loss is always logged; blocks entries only when
           ``portfolio_risk.gap_risk_enforce`` is true.
        """
        cfg = _seller_settings.portfolio_risk()
        for _u, _c in chains.items():
            self._observe_chain(_u, _c, today)
        self._log_greeks_advisories()
        await self._maybe_record_attribution(today, now)
        if self._realized_day != today:
            self._realized_day, self._realized_today = today, 0.0
            self._realized_today_by_strategy = {}
        unrealized = sum(self._unrealized_pnl(p, current_prices) for p in self.positions.values())
        # 2026-10-07: config/capital.json (src/risk/budget.py) is now the ONE source for
        # this threshold -- was options_settings.json portfolio_risk.daily_loss_halt_pct
        # x options_capital_rupees, which had silently drifted from the (also now
        # RiskBudget-sourced) G1 check in crm.py, each measured against a different base.
        from src.risk.budget import get_budget as _rb_get_budget
        from src.risk.budget import get_current_equity as _rb_get_equity

        _equity = _rb_get_equity()
        _all_strategies_pct = _rb_get_budget().options.max_loss_per_day_all_strategies_pct
        halted, reason = circuit_breaker_check(
            self._realized_today,
            unrealized,
            _equity,
            halt_pct=_all_strategies_pct,
        )
        if halted and self._entries_halted_day != today:
            self._entries_halted_day = today
            _LOG.error(
                "seller_desk_daily_loss_halt %s realized=%.0f unrealized=%.0f",
                reason,
                self._realized_today,
                unrealized,
            )

        self._gap_blocked = False
        for underlying in {p.underlying for p in self.positions.values()}:
            chain = chains.get(underlying)
            if chain is None:
                continue
            atm_iv = chain.atm_iv() or self._atm_iv.get(underlying)
            if atm_iv is None:
                _LOG.warning(
                    "seller_desk_gap_stress_skipped underlying=%s reason=no_atm_iv", underlying
                )
                continue
            book = [p for p in self.positions.values() if p.underlying == underlying]
            results = run_gap_stress_test(
                chain.spot,
                net_delta=sum(self._native_greeks(p).net_delta for p in book),
                net_gamma=sum(self._native_greeks(p).net_gamma for p in book),
                net_vega=sum(self._native_greeks(p).net_vega for p in book),
                atm_iv=atm_iv,
            )
            blocked, gap_reason = gap_blocking_check(
                results, self._options_capital, is_banknifty="BANKNIFTY" in underlying.upper()
            )
            _LOG.info(
                "seller_desk_gap_stress underlying=%s worst5=%.0f worst10=%.0f blocked=%s (%s)",
                underlying,
                min((r.pnl for r in results if r.gap_pct == 0.05), default=0.0),
                min((r.pnl for r in results if r.gap_pct == 0.10), default=0.0),
                blocked,
                gap_reason,
            )
            if blocked and bool(cfg.get("gap_risk_enforce", False)):
                self._gap_blocked = True

    def _unrealized_pnl(self, pos: OpenPosition, current_prices: dict[str, float]) -> float:
        total = 0.0
        for leg in pos.protection_legs + pos.income_legs:
            current = current_prices.get(leg.tradingsymbol, leg.premium)
            if leg.direction == "BUY":
                total += (current - leg.premium) * leg.quantity  # long gains as price rises
            else:
                total += (leg.premium - current) * leg.quantity  # short gains as price falls
        return total + pos.closed_pnl  # legs already stopped out still count toward the day's MTM

    async def close_single_leg(
        self,
        pos: OpenPosition,
        leg: OptionLeg,
        *,
        ltp: float | None,
        now: datetime,
        reason: str,
        trigger: float | None = None,
    ) -> bool:
        """Close ONE leg of an open position at market -- for ANY strategy -- and do everything a leg close
        needs, once, here: move it to ``pos.closed_legs`` with its exit price/time/reason, persist it (a
        restart must not re-open it), and send the realised-P&L Telegram alert. False (leg left open, the
        caller retries next tick) when the broker call fails."""
        executor = self._executor_for(self._pos_is_paper(pos)) or self._executor
        hint = getattr(executor, "hint_prices", None)
        if ltp is not None and callable(hint):
            hint({leg.tradingsymbol: ltp})  # paper: fill at the price the decision just saw
        try:
            fill = await executor.close_leg(leg, pos.position_id)
        except Exception as exc:  # noqa: BLE001 -- the caller alerts and retries; never silent
            _LOG.error(
                "seller_desk_leg_close_failed id=%s leg=%s err=%s",
                pos.position_id,
                leg.tradingsymbol,
                exc,
            )
            return False
        exit_premium = float(
            fill if fill is not None else (ltp if ltp is not None else leg.premium)
        )
        rec: dict[str, Any] = {
            "tradingsymbol": leg.tradingsymbol,
            "instrument_type": leg.instrument_type,
            "strike": leg.strike,
            "direction": leg.direction,
            "lots": leg.lots,
            "lot_size": leg.lot_size,
            "premium": leg.premium,
            "delta": leg.delta,
            "exit_premium": exit_premium,
            "exit_at": now.isoformat(),
            "reason": reason,
            "trigger": trigger,
        }
        (pos.income_legs if leg in pos.income_legs else pos.protection_legs).remove(leg)
        pos.closed_legs.append(rec)
        slip = (exit_premium - trigger) if trigger is not None else None
        _LOG.warning(
            "seller_desk_leg_closed id=%s type=%s leg=%s reason=%s entry=%.2f trigger=%s exit=%.2f "
            "slippage_vs_trigger=%s booked=%.0f",
            pos.position_id,
            pos.strategy_type,
            leg.tradingsymbol,
            reason,
            leg.premium,
            f"{trigger:.2f}" if trigger is not None else "-",
            exit_premium,
            f"{slip:+.2f}" if slip is not None else "-",
            pos.closed_pnl,
        )
        if self._repo is not None:
            try:
                await self._repo.record_leg_exit(pos.position_id, rec)
            except Exception as exc:  # noqa: BLE001 -- order already placed; restart would see the leg open
                _LOG.error("seller_desk_leg_exit_persist_failed id=%s err=%s", pos.position_id, exc)
        try:
            qty = leg.lots * leg.lot_size
            sign = 1.0 if leg.direction == "BUY" else -1.0
            notify(
                EventKind.PARTIAL_BOOK,
                is_paper=self._pos_is_paper(pos),
                agent_trade_id=pos.position_id,
                symbol=leg.tradingsymbol,
                segment=Segment.OPTIONS,
                dedupe_key=f"legclose:{pos.position_id}:{leg.tradingsymbol}",
                leg_label=f"{pos.underlying.split(':')[-1]} {leg.strike:g} {leg.instrument_type}",
                side=leg.direction,
                qty=qty,
                entry=leg.premium,
                exit=exit_premium,
                reason=reason,
                realized_pnl=sign * (exit_premium - leg.premium) * qty,
            )
        except Exception:  # notify() must never fail the trade path
            pass
        return True

    async def _close_position(self, position_id: str, *, reason: str) -> None:
        pos = self.positions.get(position_id)
        if pos is None:
            return
        realized_pnl = 0.0
        pnl_complete = True
        exit_prices: dict[str, float] = {}
        close_exec = self._executor_for(self._pos_is_paper(pos)) or self._executor
        for leg in pos.protection_legs + pos.income_legs:
            exit_price = await close_exec.close_leg(leg, position_id)
            if exit_price is None:
                pnl_complete = False
                continue
            exit_prices[leg.tradingsymbol] = exit_price
            # BUY (long) leg profits when price rises; SELL (short) leg
            # profits when price falls -- see close_leg's docstring for why
            # exit_price is an LTP-at-close approximation, not a confirmed fill.
            sign = 1.0 if leg.direction == "BUY" else -1.0
            realized_pnl += sign * (exit_price - leg.premium) * leg.quantity
        realized_pnl += (
            pos.closed_pnl
        )  # per-leg stops (options_intraday_short_premium) closed earlier
        # Per-leg realised P&L of the whole structure (legs closed earlier + the ones closed now), so the
        # EXIT alert shows where the total came from.
        leg_pnls: list[dict[str, Any]] = [
            {
                "leg": f"{c['instrument_type']} {float(c['strike']):g}",
                "pnl": (1.0 if c.get("direction") == "BUY" else -1.0)
                * (float(c["exit_premium"]) - float(c["premium"]))
                * int(c.get("lots", 1))
                * int(c.get("lot_size", 1)),
            }
            for c in pos.closed_legs
        ]
        for lg in pos.protection_legs + pos.income_legs:
            if lg.tradingsymbol in exit_prices:
                leg_pnls.append(
                    {
                        "leg": f"{lg.instrument_type} {lg.strike:g}",
                        "pnl": (1.0 if lg.direction == "BUY" else -1.0)
                        * (exit_prices[lg.tradingsymbol] - lg.premium)
                        * lg.quantity,
                    }
                )
        charges: dict[str, float] | None = None
        _broker: str | None = None
        if pnl_complete:
            # Entry + exit order per leg, legs stopped out earlier included. realized_pnl stays gross.
            from src.risk.real_pnl import active_broker, options_trade_charges

            _broker = active_broker(for_paper=self._pos_is_paper(pos))
            charge_legs = [
                (
                    str(c["direction"]),
                    float(c["premium"]),
                    float(c["exit_premium"]),
                    int(c.get("lots", 1)) * int(c.get("lot_size", 1)),
                )
                for c in pos.closed_legs
            ] + [
                (lg.direction, lg.premium, exit_prices[lg.tradingsymbol], lg.quantity)
                for lg in pos.protection_legs + pos.income_legs
                if lg.tradingsymbol in exit_prices
            ]
            charges = options_trade_charges(charge_legs, _broker).as_dict()
        del self.positions[position_id]
        if pnl_complete:
            self._add_realized(realized_pnl, pos.strategy_type)
        if self._repo is not None:
            try:
                await self._repo.close_position(
                    position_id,
                    exit_reason=reason,
                    closed_at=datetime.now(_IST),
                    realized_pnl=realized_pnl if pnl_complete else None,
                    exit_prices=exit_prices,
                    charges_breakdown=charges,
                    broker=_broker,
                )
            except Exception as exc:  # noqa: BLE001 — persistence is best-effort, never blocks a close
                _LOG.warning(
                    "seller_desk_position_close_persist_failed id=%s err=%s", position_id, exc
                )
        _LOG.warning(
            "seller_desk_position_closed id=%s reason=%s realized_pnl=%s",
            position_id,
            reason,
            f"{realized_pnl:.0f}" if pnl_complete else "incomplete",
        )
        await self._run_autopsy(pos, realized_pnl if pnl_complete else None)
        try:
            # Multi-leg structure -- no single entry/exit price to show (unlike
            # format_exit's equity entry_px->exit_px line), so those fields are
            # omitted; net_pnl covers it. A leg whose LTP-at-close fetch failed
            # (pnl_complete=False) leaves realized_pnl partial -- reported as 0
            # rather than a silently-wrong partial figure.
            notify(
                EventKind.EXIT,
                is_paper=self._pos_is_paper(pos),
                agent_trade_id=position_id,
                symbol=pos.underlying,
                segment=Segment.OPTIONS,
                dedupe_key=f"exit:{position_id}",
                reason=reason,
                net_pnl=realized_pnl if pnl_complete else 0.0,
                leg_pnls=leg_pnls,
            )
        except Exception:  # notify() must never fail the trade path
            pass
