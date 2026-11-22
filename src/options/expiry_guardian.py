"""Seller-desk expiry guardian — 4-layer DTE safety net (options_spec.md §6).

Not optional: near-expiry gamma risk is non-linear and explosive. THE most
expensive mistake in Indian options is a short option expiring even 1 point
ITM — physical settlement + STT on full notional can turn a winning trade
into a large loss (see spec §6's STT trap). Layer 0 (expiry-day close) takes
absolute priority over every other subsystem — position manager, margin
monitor, adjustment engine — on expiry day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from src.market import sessions as _sessions

_LAYER1_DTE = 3
_LAYER2_DTE = 2
_LAYER3_DTE = 1
_STAGE1_TIME = _sessions.hhmm(_sessions.market_config("nse_fo")["expiry_guardian"]["stage1"])
_STAGE2_TIME = _sessions.hhmm(_sessions.market_config("nse_fo")["expiry_guardian"]["stage2"])


@dataclass
class GuardianAction:
    close_now: bool
    order_type: str  # "LIMIT" | "MARKET" | "NONE"
    reason: str
    layer: str


def evaluate(dte: int, now_time: time, *, is_expiry_day: bool) -> GuardianAction:
    """Which guardian layer applies right now for a position at this DTE."""
    if is_expiry_day:
        if now_time >= _STAGE2_TIME:
            return GuardianAction(
                True, "MARKET", "EXPIRY_DAY_STT_PREVENTION_FORCE_CLOSE", "LAYER0_STAGE2"
            )
        if now_time >= _STAGE1_TIME:
            return GuardianAction(True, "LIMIT", "EXPIRY_DAY_ORDERLY_CLOSE", "LAYER0_STAGE1")
        # Any open position before 15:00 on expiry day is already a Layer4
        # violation (must be zero within 15min of market open) — close now,
        # don't wait for the 15:00 window.
        return GuardianAction(True, "MARKET", "EXPIRY_DAY_ABSOLUTE_CLOSE", "LAYER4")

    if dte <= _LAYER3_DTE:
        return GuardianAction(True, "MARKET", "EXPIRY_EMERGENCY_DTE1", "LAYER3")
    if dte <= _LAYER2_DTE:
        return GuardianAction(True, "MARKET", "DTE2_INVARIANT_FORCE_CLOSE", "LAYER2")
    if dte <= _LAYER1_DTE:
        return GuardianAction(True, "LIMIT", "GAMMA_RISK_DTE3_EXIT", "LAYER1")
    return GuardianAction(False, "NONE", f"dte={dte} outside guardian window", "NONE")


def otm_long_near_money(
    spot: float, strike: float, kind: str, *, threshold_pct: float = 0.005
) -> bool:
    """True if a long OTM option is close enough to money that forced exercise
    into an unwanted futures position is a real risk (§6 Stage 2 also closes
    these, alongside the orderly/forced short closes)."""
    if kind.upper() == "CE":
        if strike <= spot:
            return False  # already ITM, not the OTM case this check targets
        distance = (strike - spot) / spot
    else:
        if strike >= spot:
            return False
        distance = (spot - strike) / spot
    return distance <= threshold_pct
