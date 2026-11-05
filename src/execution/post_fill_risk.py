"""
Post-fill risk verification — slippage-induced risk-cap breach + SL tightening.

Per docs/05-order-lifecycle.md (Price Truth, Phase 1):
  After the actual fill price is known, recompute risk from the ACTUAL entry:
    actual_risk_rupees = |fill_price - current_sl| * quantity
  If actual_risk_rupees > max_risk_per_trade_rupees (operator.json hard cap):
    → SLIPPAGE_RISK_BREACH incident
    → tighten the SL so risk returns to within the cap.

This module is pure computation — it makes NO broker/DB calls. The caller
(OrderLifecycleManager) applies the suggested SL via broker.modify_order and
raises the incident via SupervisorClient.

Tightened SL maths (keeps risk == cap exactly):
  LONG  (BUY) : SL sits below entry → raise it:  new_sl = fill_price - cap/qty
  SHORT (SELL): SL sits above entry → lower it:  new_sl = fill_price + cap/qty
"""

from __future__ import annotations

from dataclasses import dataclass

INCIDENT_SLIPPAGE_RISK_BREACH = "SLIPPAGE_RISK_BREACH"


def _is_long(direction: str) -> bool:
    """Entry direction → True for long. Accepts BUY/long, SELL/short."""
    return direction.upper() in ("BUY", "LONG")


@dataclass(frozen=True)
class PostFillRiskResult:
    """Outcome of a post-fill risk check."""

    breached: bool
    actual_risk_rupees: float
    max_risk_rupees: float
    suggested_sl: float  # tightened SL on breach; == current_sl otherwise
    incident_code: str  # INCIDENT_SLIPPAGE_RISK_BREACH on breach, else ""
    message: str


def verify_post_fill_risk(
    *,
    fill_price: float,
    current_sl: float,
    quantity: int,
    direction: str,
    max_risk_rupees: float,
) -> PostFillRiskResult:
    """
    Recompute risk from the actual fill price and flag/tighten if over the cap.

    Args:
        fill_price: Actual broker fill price (the truth), rupees.
        current_sl: Stop-loss price currently set.
        quantity: Filled quantity (> 0).
        direction: "BUY" (long) or "SELL" (short) — the ENTRY direction.
        max_risk_rupees: Operator hard cap per trade (> 0).

    Raises:
        ValueError: if quantity <= 0 or max_risk_rupees <= 0.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be > 0, got {quantity}")
    if max_risk_rupees <= 0:
        raise ValueError(f"max_risk_rupees must be > 0, got {max_risk_rupees}")

    actual_risk = abs(fill_price - current_sl) * quantity

    if actual_risk <= max_risk_rupees:
        return PostFillRiskResult(
            breached=False,
            actual_risk_rupees=actual_risk,
            max_risk_rupees=max_risk_rupees,
            suggested_sl=current_sl,
            incident_code="",
            message=(
                f"Post-fill risk OK: {actual_risk:.0f} <= cap {max_risk_rupees:.0f} "
                f"(fill={fill_price:.2f} sl={current_sl:.2f} qty={quantity})"
            ),
        )

    max_sl_distance = max_risk_rupees / quantity
    is_long = _is_long(direction)
    suggested_sl = fill_price - max_sl_distance if is_long else fill_price + max_sl_distance

    return PostFillRiskResult(
        breached=True,
        actual_risk_rupees=actual_risk,
        max_risk_rupees=max_risk_rupees,
        suggested_sl=suggested_sl,
        incident_code=INCIDENT_SLIPPAGE_RISK_BREACH,
        message=(
            f"SLIPPAGE_RISK_BREACH: actual risk {actual_risk:.0f} > cap {max_risk_rupees:.0f} "
            f"(fill={fill_price:.2f} sl={current_sl:.2f} qty={quantity}); "
            f"tighten SL {current_sl:.2f} → {suggested_sl:.2f}"
        ),
    )


@dataclass(frozen=True)
class ReanchorResult:
    """Outcome of re-anchoring SL+target to the actual fill price."""

    sl: float  # re-anchored stop-loss (cap-tightened if a cap was supplied)
    target: float  # re-anchored target
    risk_rupees: float  # |fill - sl| * quantity (or |fill - sl| if qty unknown)
    cap_breached: bool  # True if the cap forced an extra SL tighten
    degenerate: bool  # True if signal geometry was invalid (risk/ratio <= 0)
    incident_code: str  # INCIDENT_SLIPPAGE_RISK_BREACH on cap breach, else ""
    message: str


def reanchor_protection(
    *,
    fill_price: float,
    signal_entry: float,
    signal_sl: float,
    signal_target: float,
    direction: str,
    quantity: int = 0,
    max_risk_rupees: float = 0.0,
) -> ReanchorResult:
    """
    Re-anchor SL and target to the ACTUAL fill, preserving the signal's R:R.

    The signal computes entry/SL/target off a reference level (e.g. the ORB
    breakout). A MARKET order fills at the live LTP, which may sit anywhere —
    even past the signal target. Arming protection at the stale signal levels
    then fires instantly (the EICHERMOT "TARGET_HIT at a loss" bug). Re-anchoring
    off the fill guarantees valid geometry (sl < fill < target for a long).

    Args:
        fill_price: Actual fill price (the truth), rupees.
        signal_entry/signal_sl/signal_target: The signal's planned levels.
        direction: Entry direction — BUY/long or SELL/short.
        quantity: Filled quantity. Only used (with max_risk_rupees) for the cap
            tighten; pass 0 to skip the cap check (re-anchor only).
        max_risk_rupees: Operator per-trade hard cap. Pass 0 to skip the cap
            tighten (paper path has no cap to enforce here).

    Returns:
        ReanchorResult. On degenerate signal geometry (risk <= 0 or ratio <= 0)
        the original levels are echoed back with degenerate=True so the caller
        can skip arming / flatten rather than trust junk levels.
    """
    risk = abs(signal_entry - signal_sl)
    ratio = abs(signal_target - signal_entry) / risk if risk > 0 else 0.0
    if risk <= 0 or ratio <= 0:
        return ReanchorResult(
            sl=signal_sl,
            target=signal_target,
            risk_rupees=0.0,
            cap_breached=False,
            degenerate=True,
            incident_code="",
            message=(
                f"degenerate signal geometry: entry={signal_entry:.2f} sl={signal_sl:.2f} "
                f"target={signal_target:.2f} (risk={risk:.2f} ratio={ratio:.2f}) — skip arming"
            ),
        )

    is_long = _is_long(direction)
    if is_long:
        sl = fill_price - risk
        # Positive slippage (filled higher than the signal price) must not silently
        # tighten the stop past where the signal placed it — that shift is what
        # turned a normal pullback into an instant stop-out (APOLLOHOSP 2026-07-30:
        # SL moved 8927.15→8930.63 on +3.48 slippage, clipped by a bar low of 8930.0,
        # then price recovered the next bar).
        sl = min(sl, signal_sl)
        target = fill_price + ratio * risk
    else:
        sl = fill_price + risk
        sl = max(sl, signal_sl)
        target = fill_price - ratio * risk

    cap_breached = False
    incident_code = ""
    if max_risk_rupees > 0 and quantity > 0:
        pfr = verify_post_fill_risk(
            fill_price=fill_price,
            current_sl=sl,
            quantity=quantity,
            direction=direction,
            max_risk_rupees=max_risk_rupees,
        )
        if pfr.breached:
            sl = pfr.suggested_sl
            cap_breached = True
            incident_code = pfr.incident_code

    risk_rupees = abs(fill_price - sl) * quantity if quantity > 0 else abs(fill_price - sl)
    return ReanchorResult(
        sl=sl,
        target=target,
        risk_rupees=risk_rupees,
        cap_breached=cap_breached,
        degenerate=False,
        incident_code=incident_code,
        message=(
            f"reanchored to fill={fill_price:.2f}: sl {signal_sl:.2f}→{sl:.2f} "
            f"target {signal_target:.2f}→{target:.2f} (R:R={ratio:.2f} risk={risk:.2f})"
        ),
    )
