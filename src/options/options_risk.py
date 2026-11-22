"""Options risk gate (Phase 6, Layer 4).

Enforces every parameter in ``execution.json → options_execution`` on a chosen
options :class:`StructureChoice` before it is placed. Runs *after* structure
selection and the equity CRM gates — it is additive, never a bypass.

Equity choices pass through untouched (the equity CRM pipeline already gated
them); this gate only constrains options structures.

NOTE: this gate is the buyer-first structure-substitution path's gate ONLY
(orchestrator.py _try_options_structure). Its avoid_naked_selling /
_is_naked() check hard-rejects any net-credit or uncovered-short structure
-- that is exactly what the seller-first desk (seller_desk.py, ASHWAX)
writes (iron condors, credit spreads). Do NOT wire this gate into the
seller desk; that engine's risk gate is src/risk/crm.py via
src/options/seller_crm_adapter.py, not this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.options.nfo_chain import OptionChain, iv_percentile, liquidity_ok
from src.options.structure_selector import StructureChoice

_LOG = logging.getLogger("options.options_risk")


@dataclass
class OptionsGateResult:
    passed: bool
    reason: str = ""


class OptionsRiskGate:
    def __init__(self, options_cfg: dict[str, Any] | None = None) -> None:
        cfg = options_cfg or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._max_premium = float(cfg.get("max_premium_at_risk_rupees", 5000))
        self._max_delta = float(cfg.get("max_delta_exposure", 0.7))
        self._avoid_naked = bool(cfg.get("avoid_naked_selling", True))
        self._min_dte = int(cfg.get("min_days_to_expiry", 14))
        self._max_iv_buy = float(cfg.get("max_iv_percentile_for_buying", 70))
        self._min_oi = int(cfg.get("liquidity_min_oi", 1000))
        self._min_volume = int(cfg.get("liquidity_min_volume", 500))

    def verify(
        self,
        choice: StructureChoice,
        chain: OptionChain | None,
        iv_history: list[float] | None = None,
    ) -> OptionsGateResult:
        # Equity is not an options structure — nothing for this gate to do.
        if not choice.legs:
            return OptionsGateResult(True, "equity — options gate not applicable")

        if not self._enabled:
            return OptionsGateResult(False, "options_execution disabled")
        if chain is None:
            return OptionsGateResult(False, "no option chain context")

        # Premium at risk.
        if choice.max_loss > self._max_premium:
            return OptionsGateResult(
                False,
                f"max_loss {choice.max_loss:.0f} > max_premium {self._max_premium:.0f}",
            )

        # Net delta exposure (per share).
        if abs(choice.net_delta) > self._max_delta:
            return OptionsGateResult(
                False,
                f"net_delta {choice.net_delta:.2f} > max_delta {self._max_delta:.2f}",
            )

        # Avoid naked selling: must be net-debit and every short leg covered.
        if self._avoid_naked:
            naked = self._is_naked(choice)
            if naked:
                return OptionsGateResult(False, "naked short leg / net-credit structure")

        # Days to expiry.
        if chain.days_to_expiry < self._min_dte:
            return OptionsGateResult(False, f"dte {chain.days_to_expiry} < min {self._min_dte}")

        # IV percentile — all preferred structures are net-long premium (buying);
        # block when IV is too rich or the percentile is unknown (fail-safe).
        pct = iv_percentile(chain.atm_iv(), iv_history or [])
        if pct is None:
            return OptionsGateResult(False, "IV percentile unavailable")
        if pct > self._max_iv_buy:
            return OptionsGateResult(False, f"IV pct {pct:.0f} > max_buy {self._max_iv_buy:.0f}")

        # Per-leg liquidity.
        liq = self._liquidity_ok(choice, chain)
        if not liq:
            return OptionsGateResult(False, "leg liquidity below OI/volume floor")

        return OptionsGateResult(True, "options structure cleared")

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_naked(choice: StructureChoice) -> bool:
        """True if any short leg is uncovered or the book is net-credit."""
        buy_lots = sum(leg.lots for leg in choice.legs if leg.direction == "BUY")
        sell_lots = sum(leg.lots for leg in choice.legs if leg.direction == "SELL")
        if sell_lots > buy_lots:
            return True  # more short than long contracts → uncovered
        # Net premium must be a debit (we pay), never a credit (we receive).
        net_premium = sum(
            (1.0 if leg.direction == "BUY" else -1.0) * leg.premium for leg in choice.legs
        )
        return net_premium <= 0.0

    def _liquidity_ok(self, choice: StructureChoice, chain: OptionChain) -> bool:
        by_strike = {row.strike: row for row in chain.strikes}
        for leg in choice.legs:
            row = by_strike.get(leg.strike)
            if row is None:
                return False
            quote = row.call if leg.instrument_type == "CE" else row.put
            if not liquidity_ok(quote, self._min_oi, self._min_volume):
                return False
        return True
