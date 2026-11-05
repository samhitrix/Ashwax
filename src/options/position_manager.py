"""Seller-desk position manager — 5-min per-position monitoring (options_spec.md §10).

Pure decision layer: given a position's current state, returns which
management action(s) trigger. Running this on a 5-min schedule and placing
the resulting close/adjust orders is orchestrator wiring (see
seller_desk.sweep_position_manager).

Rule 6 ("never add to a losing options position — no averaging, no
doubling") has no function here because there is no "add to position" code
path anywhere in this build to guard against — the invariant is enforced by
omission, not by a check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.options import seller_settings

if TYPE_CHECKING:
    from src.options.structure_selector import OptionLeg

_DELTA_BREACH_NORMAL = 0.35
_DELTA_BREACH_BLACKOUT = 0.25
# Short-strike |delta| at which a single-sided credit spread (bull_put/bear_call, nothing to roll) is
# closed. Default = the spec's 0.35. A looser value (0.50, "close only once tested") was proposed
# 2026-10-13 but scripts/replay_vertical_delta_breach.py found NO consistent edge over 0.35 (train and
# hold-out disagree; 0.50 doubles the average loss and worsens the worst), so it stays at 0.35 until
# evidence says otherwise. Tune via config adjustments.single_sided_tested_delta.
_DELTA_TESTED_SINGLE_SIDED = 0.35
_CREDIT_PROFIT_TARGET_PCT = 50.0
_CREDIT_PROFIT_TARGET_BLACKOUT_PCT = 35.0
_CREDIT_STOP_LOSS_PCT = 150.0
_DEBIT_PROFIT_TARGET_PCT = 60.0
_DEBIT_STOP_LOSS_PCT = 100.0
_DEBIT_BREAKEVEN_LOCK_ARM_PCT = 35.0
_IV_CRUSH_EARLY_EXIT_PCT = 25.0
_DTE_DOUBLE_CHECK = 3


def _pct(key: str, default: float) -> float:
    """config/options_settings.json ``profit_and_stop.<key>``; the module constants
    above are the spec's own defaults when a key is absent."""
    return float(seller_settings.profit_and_stop().get(key, default))


@dataclass
class ManagementAction:
    action: str  # "ADJUST" | "CLOSE" | "NONE"
    reason: str
    breached_instrument_type: str | None = None  # "CE" | "PE" — which income leg breached


def _breach_thresholds() -> tuple[float, float]:
    """(normal, blackout) from config adjustments.delta_breach_threshold / delta_breach_blackout."""
    cfg = seller_settings.adjustment_settings()
    return (
        float(cfg.get("delta_breach_threshold", _DELTA_BREACH_NORMAL)),
        float(cfg.get("delta_breach_blackout", _DELTA_BREACH_BLACKOUT)),
    )


def single_sided_tested_delta(*, blackout_active: bool = False) -> float:
    """Breach delta for a single-sided credit spread (config adjustments.single_sided_tested_delta).
    On a blackout day it is tightened by the same margin as the condor rule (normal - blackout),
    so a looser normal-day value never relaxes the event-day protection."""
    tested = float(
        seller_settings.adjustment_settings().get(
            "single_sided_tested_delta", _DELTA_TESTED_SINGLE_SIDED
        )
    )
    if not blackout_active:
        return tested
    normal, blackout = _breach_thresholds()
    return max(blackout, tested - (normal - blackout))


def check_delta_breach(
    short_strike_delta: float, *, blackout_active: bool, threshold: float | None = None
) -> ManagementAction:
    """``threshold`` overrides the configured normal / blackout thresholds when given."""
    if threshold is None:
        normal, blackout = _breach_thresholds()
        threshold = blackout if blackout_active else normal
    if abs(short_strike_delta) > threshold:
        return ManagementAction("ADJUST", f"delta {abs(short_strike_delta):.2f} > {threshold:.2f}")
    return ManagementAction("NONE", "delta within threshold")


def check_delta_breach_any_leg(
    income_legs: list[OptionLeg], *, blackout_active: bool, threshold: float | None = None
) -> ManagementAction:
    """Checks EVERY income leg's delta, not just one — an Iron Condor has two
    (short put + short call) and the tested side is whichever one the market
    actually moved toward. Returns the worst (largest |delta|) breach, or
    NONE if every leg is within threshold. The returned action carries
    ``breached_instrument_type`` so the caller can identify the untested
    side to roll (options_spec.md §10's "roll the untested side")."""
    worst: ManagementAction | None = None
    worst_abs_delta = 0.0
    for leg in income_legs:
        result = check_delta_breach(leg.delta, blackout_active=blackout_active, threshold=threshold)
        if result.action == "ADJUST" and abs(leg.delta) > worst_abs_delta:
            worst_abs_delta = abs(leg.delta)
            worst = ManagementAction(
                result.action, result.reason, breached_instrument_type=leg.instrument_type
            )
    return worst if worst is not None else ManagementAction("NONE", "delta within threshold")


def check_profit_target(
    direction: str, unrealized_pnl: float, max_potential: float, *, blackout_active: bool = False
) -> ManagementAction:
    if max_potential <= 0.0:
        return ManagementAction("NONE", "max_potential unavailable")
    pct_captured = unrealized_pnl / max_potential * 100.0
    target = (
        (
            _pct("credit_profit_target_blackout_pct", _CREDIT_PROFIT_TARGET_BLACKOUT_PCT)
            if blackout_active
            else _pct("credit_profit_target_pct", _CREDIT_PROFIT_TARGET_PCT)
        )
        if direction == "credit"
        else _pct("debit_profit_target_pct", _DEBIT_PROFIT_TARGET_PCT)
    )
    if pct_captured >= target:
        return ManagementAction("CLOSE", f"PROFIT_TARGET_HIT {pct_captured:.0f}% >= {target:.0f}%")
    return ManagementAction("NONE", f"profit {pct_captured:.0f}% below target {target:.0f}%")


def check_stop_loss(direction: str, unrealized_pnl: float, premium: float) -> ManagementAction:
    if premium <= 0.0:
        return ManagementAction("NONE", "premium unavailable")
    pct = (
        _pct("credit_stop_loss_pct", _CREDIT_STOP_LOSS_PCT)
        if direction == "credit"
        else _pct("debit_stop_loss_pct", _DEBIT_STOP_LOSS_PCT)
    )
    loss_threshold = -pct / 100.0 * premium
    if unrealized_pnl <= loss_threshold:
        label = "credit loss" if direction == "credit" else "debit loss"
        return ManagementAction("CLOSE", f"STOP_LOSS_HIT {label} >= {pct:.0f}% of premium")
    return ManagementAction("NONE", "within stop-loss")


def check_breakeven_lock(
    direction: str, unrealized_pnl: float, peak_unrealized_pnl: float | None, premium: float
) -> ManagementAction:
    """Debit structures only: once the position has been up ``debit_breakeven_lock_arm_pct``
    of the debit paid, never let a winner turn into a loser -- exit if P&L returns to
    zero. Without this a paper profit just sits unrealized and reverses (found
    2026-09-28: a bull_call_spread was in profit, then bled to -55..-70% of its debit)."""
    if direction != "debit" or peak_unrealized_pnl is None or premium <= 0.0:
        return ManagementAction("NONE", "not applicable")
    arm_pct = _pct("debit_breakeven_lock_arm_pct", _DEBIT_BREAKEVEN_LOCK_ARM_PCT)
    peak_pct = peak_unrealized_pnl / premium * 100.0
    if peak_pct >= arm_pct and unrealized_pnl <= 0.0:
        return ManagementAction(
            "CLOSE", f"BREAKEVEN_LOCK peak +{peak_pct:.0f}% of debit given back to breakeven"
        )
    return ManagementAction("NONE", "breakeven lock not triggered")


def check_dte(dte: int) -> ManagementAction:
    if dte <= _DTE_DOUBLE_CHECK:
        return ManagementAction(
            "CLOSE", f"GAMMA_RISK_DTE3_EXIT dte={dte} (expiry_guardian double-check)"
        )
    return ManagementAction("NONE", "dte outside guardian window")


def check_iv_crush(direction: str, entry_iv: float, current_iv: float) -> ManagementAction:
    if direction != "debit" or entry_iv <= 0.0:
        return ManagementAction("NONE", "not applicable")
    drop_pct = (entry_iv - current_iv) / entry_iv * 100.0
    if drop_pct > _pct("iv_crush_early_exit_pct", _IV_CRUSH_EARLY_EXIT_PCT):
        return ManagementAction("CLOSE", f"IV_CRUSH_EARLY_EXIT drop={drop_pct:.0f}%")
    return ManagementAction("NONE", f"iv drop {drop_pct:.0f}% below threshold")


def evaluate_position(
    *,
    direction: str,
    short_strike_delta: float | None,
    unrealized_pnl: float,
    max_potential: float,
    premium: float,
    dte: int,
    entry_iv: float,
    current_iv: float,
    blackout_active: bool = False,
    peak_unrealized_pnl: float | None = None,
) -> list[ManagementAction]:
    """Runs all checks (§10, order 1-5) and returns every triggered action —
    the caller decides priority/dedup when applying them (e.g. CLOSE should
    generally win over ADJUST if both fire)."""
    actions: list[ManagementAction] = []
    if short_strike_delta is not None:
        a = check_delta_breach(short_strike_delta, blackout_active=blackout_active)
        if a.action != "NONE":
            actions.append(a)
    for a in (
        check_profit_target(
            direction, unrealized_pnl, max_potential, blackout_active=blackout_active
        ),
        check_stop_loss(direction, unrealized_pnl, premium),
        check_breakeven_lock(direction, unrealized_pnl, peak_unrealized_pnl, premium),
        check_dte(dte),
        check_iv_crush(direction, entry_iv, current_iv),
    ):
        if a.action != "NONE":
            actions.append(a)
    return actions
