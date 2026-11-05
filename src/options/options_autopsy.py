"""Seller-desk options autopsy — options_spec.md §18.

Per closed position, answers the spec's 7 diagnostic questions and returns
findings for ``options_learning_log`` (migration 056). Automatic feedback
into strategy_selector's thresholds ("Iron Condors on BANKNIFTY lose when
ADX > X" → tighten filter) is NOT implemented — that requires a calibration
system operating across many trades' history, out of scope for a per-trade
autopsy. This module produces the raw findings that system would consume.
"""

from __future__ import annotations

from dataclasses import dataclass

_EARLY_EXIT_UNDERPERFORM_THRESHOLD_PCT = 10.0


@dataclass
class Finding:
    question: str
    finding: str
    confidence: float


def regime_correctness(regime_at_entry: str, regime_turned_trending: bool) -> Finding:
    if regime_turned_trending and regime_at_entry in (
        "LOW_VOL_RANGE_BOUND",
        "HIGH_VOL_RANGE_BOUND",
    ):
        return Finding(
            "correct_strategy_for_regime",
            f"sold in {regime_at_entry} which turned trending — regime classifier threshold may be too loose",
            0.7,
        )
    return Finding(
        "correct_strategy_for_regime",
        f"regime {regime_at_entry} held; no misclassification signal",
        0.5,
    )


def strike_delta_appropriateness(target_delta: float, was_breached: bool) -> Finding:
    if was_breached:
        return Finding(
            "strike_delta_appropriateness",
            f"{target_delta:.2f}-delta short strike was breached — consider a lower target delta",
            0.6,
        )
    return Finding(
        "strike_delta_appropriateness", f"{target_delta:.2f}-delta short strike held", 0.5
    )


def vol_thesis_correctness(sold_expecting_iv_drop: bool, iv_change_pct: float) -> Finding:
    thesis_correct = (sold_expecting_iv_drop and iv_change_pct < 0) or (
        not sold_expecting_iv_drop and iv_change_pct >= 0
    )
    if thesis_correct:
        return Finding(
            "vol_thesis_correctness", f"vol thesis played out (iv_change={iv_change_pct:.1f}%)", 0.7
        )
    return Finding(
        "vol_thesis_correctness",
        f"vol thesis WRONG (iv_change={iv_change_pct:.1f}%) — IVR threshold may need revisiting",
        0.7,
    )


def early_profit_taking_analysis(
    closed_at_pct_of_max: float, would_have_captured_holding_to_expiry_pct: float
) -> Finding:
    delta = would_have_captured_holding_to_expiry_pct - closed_at_pct_of_max
    if delta > _EARLY_EXIT_UNDERPERFORM_THRESHOLD_PCT:
        return Finding(
            "early_profit_taking",
            f"closing at {closed_at_pct_of_max:.0f}% left {delta:.0f}pp on the table vs holding to expiry",
            0.6,
        )
    return Finding(
        "early_profit_taking",
        f"closing at {closed_at_pct_of_max:.0f}% was within {abs(delta):.0f}pp of the to-expiry outcome",
        0.6,
    )


def adjustment_effectiveness(
    adjustments_made: int, pnl_with_adjustments: float, estimated_pnl_without_adjustment: float
) -> Finding:
    if adjustments_made == 0:
        return Finding("adjustment_effectiveness", "no adjustments made", 1.0)
    helped = pnl_with_adjustments > estimated_pnl_without_adjustment
    verdict = "helped" if helped else "hurt"
    return Finding(
        "adjustment_effectiveness",
        f"{adjustments_made} adjustment(s) {verdict}: actual={pnl_with_adjustments:.0f} "
        f"vs no-adjustment estimate={estimated_pnl_without_adjustment:.0f}",
        0.5,
    )


def greeks_pnl_source(
    delta_pnl: float, gamma_pnl: float, theta_pnl: float, vega_pnl: float
) -> Finding:
    contributions = {"delta": delta_pnl, "gamma": gamma_pnl, "theta": theta_pnl, "vega": vega_pnl}
    dominant = max(contributions, key=lambda k: abs(contributions[k]))
    if dominant == "theta" and theta_pnl > 0:
        note = "theta-dominant and positive — the selling edge is real for this trade"
    elif dominant == "vega" and vega_pnl < 0:
        note = "vega-dominant and negative — vol movement ate the theta gain"
    else:
        note = f"{dominant}-dominant ({contributions[dominant]:.0f})"
    return Finding("greeks_pnl_source", note, 0.6)


def run_autopsy(
    *,
    regime_at_entry: str | None,
    regime_turned_trending: bool | None,
    target_delta: float | None,
    short_strike_was_breached: bool,
    sold_expecting_iv_drop: bool,
    iv_change_pct: float | None,
    closed_at_pct_of_max: float | None,
    would_have_captured_holding_to_expiry_pct: float | None,
    adjustments_made: int,
    pnl_with_adjustments: float,
    estimated_pnl_without_adjustment: float | None,
    delta_pnl: float | None,
    gamma_pnl: float | None,
    theta_pnl: float | None,
    vega_pnl: float | None,
) -> list[Finding]:
    """Runs every autopsy question that has the inputs it needs and returns the findings.

    A ``None`` input means "not knowable for this position" (e.g. the counterfactual
    "what if we had held to expiry" is unknowable at close time; a position opened before
    entry context was recorded has no entry regime/IV). That question is skipped rather
    than answered with a fabricated number.
    """
    findings: list[Finding] = []
    if regime_at_entry is not None and regime_turned_trending is not None:
        findings.append(regime_correctness(regime_at_entry, regime_turned_trending))
    if target_delta is not None:
        findings.append(strike_delta_appropriateness(target_delta, short_strike_was_breached))
    if iv_change_pct is not None:
        findings.append(vol_thesis_correctness(sold_expecting_iv_drop, iv_change_pct))
    if closed_at_pct_of_max is not None and would_have_captured_holding_to_expiry_pct is not None:
        findings.append(
            early_profit_taking_analysis(
                closed_at_pct_of_max, would_have_captured_holding_to_expiry_pct
            )
        )
    if adjustments_made == 0 or estimated_pnl_without_adjustment is not None:
        findings.append(
            adjustment_effectiveness(
                adjustments_made, pnl_with_adjustments, estimated_pnl_without_adjustment or 0.0
            )
        )
    else:
        findings.append(
            Finding(
                "adjustment_effectiveness",
                f"{adjustments_made} adjustment(s) made; final pnl={pnl_with_adjustments:.0f}; "
                "no no-adjustment counterfactual available",
                0.3,
            )
        )
    if (
        delta_pnl is not None
        and gamma_pnl is not None
        and theta_pnl is not None
        and vega_pnl is not None
    ):
        findings.append(greeks_pnl_source(delta_pnl, gamma_pnl, theta_pnl, vega_pnl))
    return findings
