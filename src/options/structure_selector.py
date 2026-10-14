"""Structure selection — the core of master spec §11.

For every approved opportunity, choose the optimal *participation structure*:
equity vs a defined-risk options structure (``bull_call_spread`` / ``long_call`` /
``protective_put``), using the five factors from §11 — directional conviction,
time expectation, volatility (IV) environment, capital efficiency, and portfolio
state — and selecting the structure with the best risk-adjusted expected value.

This module is pure and synchronous: the caller builds the :class:`OptionChain`
(async I/O) and passes it in. Only structures in ``preferred_strategies`` are
ever produced, and ``avoid_naked_selling`` is honoured structurally — every
produced structure is net-long premium / defined-risk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.options.nfo_chain import OptionChain, iv_percentile

_LOG = logging.getLogger("options.structure_selector")

# Earnings within this many days counts as a "defined event" (§11 factor 1/2).
_EVENT_WINDOW_DAYS = 12


@dataclass
class Opportunity:
    """An approved opportunity, distilled from the CRM signal + context."""

    symbol: str  # "NSE:RELIANCE"
    direction: str  # "BUY" | "SELL"
    conviction: float  # 0..1 (LLM confidence)
    entry_price: float
    stop_loss_pct: float  # e.g. 0.025
    target_pct: float  # e.g. 0.073
    risk_rupees: float  # per-trade risk budget (capital at risk)
    time_horizon: str = "intraday"  # "intraday" | "swing"
    earnings_proximity_days: int = 999


@dataclass
class PortfolioState:
    open_position_count: int = 0
    max_open_positions: int = 5
    sector_concentrated: bool = False


@dataclass
class OptionLeg:
    tradingsymbol: str  # "NFO:RELIANCE24JUL2800CE"
    instrument_type: str  # "CE" | "PE"
    strike: float
    direction: str  # "BUY" | "SELL"
    lots: int
    lot_size: int
    premium: float  # per share
    delta: float = 0.0

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size


@dataclass
class StructureChoice:
    symbol: str
    structure_type: str  # "equity" | "bull_call_spread" | "long_call" | "protective_put"
    product: str  # "INTRADAY" | "DELIVERY" | "MARGIN"
    entry: float
    stop: float
    target: float
    capital_required: float
    max_loss: float
    max_profit: float
    rr: float
    expected_value: float
    confidence: float
    reasoning: str
    legs: list[OptionLeg] = field(default_factory=list)

    @property
    def net_delta(self) -> float:
        """Signed delta exposure (per share) across all option legs."""
        total = 0.0
        for leg in self.legs:
            sign = 1.0 if leg.direction == "BUY" else -1.0
            total += sign * leg.delta
        return total


def _p_win(
    conviction: float,
    structure_type: str | None = None,
    bounds: tuple[float, float] | None = None,
) -> float:
    """Map conviction (0..1) to a bounded win probability for EV scoring.

    Two independent, mode-gated calibration layers (Options learning-engine
    PR4/PR6), both defaulting to today's exact behavior when off:

    - structure_type + config/learning.json options_ev_calibration.mode=="live":
      sources the win-probability estimate from src.options.ev_calibration_gate's
      real backtested win_rate_pct for this structure_type instead of the raw
      conviction value, when a populated calibration entry exists. Falls back
      to conviction otherwise (off/shadow mode, or no entry for this type).
    - bounds (StructureSelector's injected conviction_bounds, populated by
      the conviction-calibration loop, PR6): replaces the hardcoded (0.45,
      0.70) clamp with a per-structure_type learned pair when
      options_conviction_calibration.mode=="live" and a pair exists — the
      caller (StructureSelector) is responsible for only passing bounds when
      that mode allows it; this function just applies whatever bounds it's given.
    """
    lo, hi = bounds if bounds is not None else (0.45, 0.70)
    base = conviction
    if structure_type is not None:
        from src.options.ev_calibration_gate import calibrated_win_rate, ev_calibration_mode

        calibrated = calibrated_win_rate(structure_type)
        if calibrated is not None and ev_calibration_mode() == "live":
            base = calibrated
    return min(hi, max(lo, base))


def _ev(p_win: float, max_profit: float, max_loss: float) -> float:
    return p_win * max_profit - (1.0 - p_win) * max_loss


class StructureSelector:
    """Selects the best participation structure for an opportunity (§11)."""

    def __init__(
        self,
        options_cfg: dict[str, Any] | None = None,
        conviction_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        cfg = options_cfg or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._preferred: list[str] = list(cfg.get("preferred_strategies", []))
        self._max_iv_buy = float(cfg.get("max_iv_percentile_for_buying", 70))
        # Options size against the *premium*-at-risk budget, not the equity
        # per-trade risk budget — premium is the defined max loss of a debit
        # structure and is capped by the same gate (execution.json).
        self._premium_budget = float(cfg.get("max_premium_at_risk_rupees", 5000))
        # Layer PR6 (conviction calibration) — per-structure_type learned
        # (p_win_floor, p_win_ceiling) pairs, replacing the hardcoded (0.45,
        # 0.70) _p_win() clamp when config/learning.json
        # options_conviction_calibration.mode=="live". Loaded once at
        # construction (orchestrator wiring time), same "load at boot" pattern
        # equity's calibration_thresholds already uses. Empty by default —
        # every _p_win() call then uses its hardcoded bounds, unchanged.
        self._conviction_bounds: dict[str, tuple[float, float]] = conviction_bounds or {}

    def _bounds_for(self, structure_type: str) -> tuple[float, float] | None:
        """config/learning.json options_conviction_calibration.mode gate for
        self._conviction_bounds — None (hardcoded 0.45/0.70 clamp) unless mode
        is "live" AND a learned bounds pair exists for this structure_type."""
        try:
            import json as _json
            from pathlib import Path as _Path

            cfg_path = _Path("config/learning.json")
            if not cfg_path.exists():
                return None
            mode = str(
                _json.loads(cfg_path.read_text(encoding="utf-8"))
                .get("options_conviction_calibration", {})
                .get("mode")
                or "off"
            )
        except Exception:
            return None
        if mode != "live":
            return None
        return self._conviction_bounds.get(structure_type)

    # ── equity baseline ──────────────────────────────────────────────────────

    def _equity_choice(self, opp: Opportunity, reason: str) -> StructureChoice:
        sl_dist = opp.entry_price * opp.stop_loss_pct
        tgt_dist = opp.entry_price * opp.target_pct
        qty = max(1, int(opp.risk_rupees / sl_dist)) if sl_dist > 0 else 1
        max_loss = sl_dist * qty
        max_profit = tgt_dist * qty
        rr = max_profit / max_loss if max_loss > 0 else 0.0
        product = "INTRADAY" if opp.time_horizon == "intraday" else "DELIVERY"
        direction_sign = 1.0 if opp.direction == "BUY" else -1.0
        stop = opp.entry_price * (1.0 - direction_sign * opp.stop_loss_pct)
        target = opp.entry_price * (1.0 + direction_sign * opp.target_pct)
        return StructureChoice(
            symbol=opp.symbol,
            structure_type="equity",
            product=product,
            entry=opp.entry_price,
            stop=stop,
            target=target,
            capital_required=opp.entry_price * qty,
            max_loss=max_loss,
            max_profit=max_profit,
            rr=rr,
            expected_value=_ev(_p_win(opp.conviction), max_profit, max_loss),
            confidence=opp.conviction,
            reasoning=reason,
            legs=[],
        )

    # ── options builders ─────────────────────────────────────────────────────

    @staticmethod
    def _lots_for_budget(max_loss_per_lot: float, budget: float) -> int:
        if max_loss_per_lot <= 0:
            return 0
        return max(0, int(budget / max_loss_per_lot))

    def _long_call(self, opp: Opportunity, chain: OptionChain) -> StructureChoice | None:
        atm = chain.atm()
        if atm is None or atm.call is None or atm.call.mid <= 0:
            return None
        lot = chain.lot_size
        premium = atm.call.mid
        max_loss_per_lot = premium * lot
        lots = self._lots_for_budget(max_loss_per_lot, self._premium_budget)
        if lots < 1:
            return None
        target_price = opp.entry_price * (1.0 + opp.target_pct)
        intrinsic_at_target = max(target_price - atm.strike, 0.0)
        max_loss = max_loss_per_lot * lots
        max_profit = (intrinsic_at_target - premium) * lot * lots
        if max_profit <= 0:
            return None
        rr = max_profit / max_loss
        leg = OptionLeg(
            tradingsymbol=atm.call_symbol,
            instrument_type="CE",
            strike=atm.strike,
            direction="BUY",
            lots=lots,
            lot_size=lot,
            premium=premium,
            delta=atm.call_delta or 0.0,
        )
        return StructureChoice(
            symbol=opp.symbol,
            structure_type="long_call",
            product="MARGIN",
            entry=premium,
            stop=premium * 0.5,  # premium-based SL (long option)
            target=premium + (intrinsic_at_target - premium),
            capital_required=max_loss,
            max_loss=max_loss,
            max_profit=max_profit,
            rr=rr,
            expected_value=_ev(
                _p_win(opp.conviction, "long_call", self._bounds_for("long_call")),
                max_profit,
                max_loss,
            ),
            confidence=opp.conviction,
            reasoning=f"long_call ATM {atm.strike:.0f}, {lots} lot(s)",
            legs=[leg],
        )

    def _bull_call_spread(self, opp: Opportunity, chain: OptionChain) -> StructureChoice | None:
        atm = chain.atm()
        if atm is None or atm.call is None or atm.call.mid <= 0:
            return None
        target_price = opp.entry_price * (1.0 + opp.target_pct)
        short_row = chain.nearest(target_price)
        if (
            short_row is None
            or short_row.call is None
            or short_row.call.mid <= 0
            or short_row.strike <= atm.strike
        ):
            return None
        lot = chain.lot_size
        net_debit = atm.call.mid - short_row.call.mid
        if net_debit <= 0:
            return None  # not a debit spread → would be net-credit (avoid)
        width = short_row.strike - atm.strike
        max_loss_per_lot = net_debit * lot
        lots = self._lots_for_budget(max_loss_per_lot, self._premium_budget)
        if lots < 1:
            return None
        max_loss = max_loss_per_lot * lots
        max_profit = (width - net_debit) * lot * lots
        if max_profit <= 0:
            return None
        rr = max_profit / max_loss
        legs = [
            OptionLeg(
                tradingsymbol=atm.call_symbol,
                instrument_type="CE",
                strike=atm.strike,
                direction="BUY",
                lots=lots,
                lot_size=lot,
                premium=atm.call.mid,
                delta=atm.call_delta or 0.0,
            ),
            OptionLeg(
                tradingsymbol=short_row.call_symbol,
                instrument_type="CE",
                strike=short_row.strike,
                direction="SELL",
                lots=lots,
                lot_size=lot,
                premium=short_row.call.mid,
                delta=short_row.call_delta or 0.0,
            ),
        ]
        return StructureChoice(
            symbol=opp.symbol,
            structure_type="bull_call_spread",
            product="MARGIN",
            entry=net_debit,
            stop=net_debit * 0.5,
            target=width,
            capital_required=max_loss,
            max_loss=max_loss,
            max_profit=max_profit,
            rr=rr,
            expected_value=_ev(
                _p_win(opp.conviction, "bull_call_spread", self._bounds_for("bull_call_spread")),
                max_profit,
                max_loss,
            ),
            confidence=opp.conviction,
            reasoning=(f"bull_call_spread {atm.strike:.0f}/{short_row.strike:.0f}, {lots} lot(s)"),
            legs=legs,
        )

    def _protective_put(self, opp: Opportunity, chain: OptionChain) -> StructureChoice | None:
        """Long equity + long OTM put hedge — defined downside."""
        sl_price = opp.entry_price * (1.0 - opp.stop_loss_pct)
        put_row = chain.nearest(sl_price)
        if put_row is None or put_row.put is None or put_row.put.mid <= 0:
            return None
        lot = chain.lot_size
        put_premium = put_row.put.mid
        # The premium-at-risk (the hedge cost) must fit the premium budget.
        if put_premium * lot > self._premium_budget:
            return None
        # Size equity to the lot so the put fully hedges it.
        qty = lot
        downside = max(opp.entry_price - put_row.strike, 0.0) * qty
        max_loss = downside + put_premium * qty
        target_price = opp.entry_price * (1.0 + opp.target_pct)
        max_profit = (target_price - opp.entry_price) * qty - put_premium * qty
        if max_profit <= 0:
            return None
        rr = max_profit / max_loss
        leg = OptionLeg(
            tradingsymbol=put_row.put_symbol,
            instrument_type="PE",
            strike=put_row.strike,
            direction="BUY",
            lots=1,
            lot_size=lot,
            premium=put_premium,
            delta=put_row.put_delta or 0.0,
        )
        return StructureChoice(
            symbol=opp.symbol,
            structure_type="protective_put",
            product="MARGIN",
            entry=opp.entry_price,
            stop=put_row.strike,
            target=target_price,
            capital_required=opp.entry_price * qty + put_premium * qty,
            max_loss=max_loss,
            max_profit=max_profit,
            rr=rr,
            expected_value=_ev(
                _p_win(opp.conviction, "protective_put", self._bounds_for("protective_put")),
                max_profit,
                max_loss,
            ),
            confidence=opp.conviction,
            reasoning=f"protective_put strike {put_row.strike:.0f}",
            legs=[leg],
        )

    # ── selection ────────────────────────────────────────────────────────────

    def select(
        self,
        opp: Opportunity,
        portfolio: PortfolioState,
        chain: OptionChain | None,
        iv_history: list[float] | None = None,
    ) -> StructureChoice:
        """Return the best structure for this opportunity per §11."""
        equity = self._equity_choice(opp, reason="equity (default)")

        # Hard rules that force equity (or are the only path):
        if not self._enabled or chain is None:
            return self._equity_choice(opp, "equity (options disabled / no chain)")
        if opp.direction == "SELL":
            # No preferred bearish options structure — equity short only.
            return self._equity_choice(opp, "equity (short; no preferred options structure)")
        if opp.time_horizon == "intraday":
            return self._equity_choice(opp, "equity MIS (intraday horizon)")

        # Factor 3 — volatility: only buy options when IV is not too rich.
        pct = iv_percentile(chain.atm_iv(), iv_history or [])
        if pct is None:
            return self._equity_choice(opp, "equity (IV percentile unavailable — fail-safe)")
        if pct > self._max_iv_buy:
            return self._equity_choice(
                opp, f"equity (IV pct {pct:.0f} > max_buy {self._max_iv_buy:.0f})"
            )

        equity_slot_available = portfolio.open_position_count < portfolio.max_open_positions
        high_conviction = opp.conviction >= 0.75
        defined_event = opp.earnings_proximity_days <= _EVENT_WINDOW_DAYS

        # Factor 1 (line 1): high conviction + tight directional setup (no event)
        # → take the equity position for full participation. Only when a slot is
        # free; otherwise fall through to a defined-risk options structure.
        if high_conviction and not defined_event and equity_slot_available:
            return self._equity_choice(opp, "equity (high conviction, tight setup)")

        # Build candidate options structures (only the preferred ones).
        builders = {
            "bull_call_spread": self._bull_call_spread,
            "long_call": self._long_call,
            "protective_put": self._protective_put,
        }
        candidates: list[StructureChoice] = []
        for name in self._preferred:
            build = builders.get(name)
            if build is None:
                continue
            choice = build(opp, chain)
            if choice is not None:
                candidates.append(choice)

        # Factor 5 — portfolio: when equity slots are full, equity isn't a real
        # option; only defined-risk structures compete. Otherwise equity competes.
        if equity_slot_available:
            candidates.append(equity)

        if not candidates:
            return self._equity_choice(opp, "equity (no buildable options structure)")

        # Factor 4 — capital efficiency: best risk-adjusted EV measured as return
        # on deployed capital (EV / capital_required). This is the §11 thesis —
        # ₹50k in options can beat ₹2L in stock on expected value per rupee.
        def _score(c: StructureChoice) -> float:
            return c.expected_value / c.capital_required if c.capital_required > 0 else 0.0

        best = max(candidates, key=_score)
        if best.structure_type != "equity":
            _LOG.info(
                "structure_selected symbol=%s type=%s rr=%.2f ev=%.0f roc=%.2f",
                opp.symbol,
                best.structure_type,
                best.rr,
                best.expected_value,
                _score(best),
            )
        return best
