"""Seller-desk gap stress test (options_spec.md §11.5).

Runs 5 gap scenarios (±1/3/5/8/10%) against every open position using an
IV-spike model calibrated to historical VIX behavior during gaps — the
calibration points are the spec's own stated numbers, not invented here.
"""

from __future__ import annotations

from dataclasses import dataclass

# (gap_pct -> iv_spike_fraction) calibration points from options_spec.md §11.5.
_IV_SPIKE_MODEL: dict[float, float] = {0.01: 0.05, 0.03: 0.15, 0.05: 0.35, 0.08: 0.55, 0.10: 0.80}
_GAP_SCENARIOS = (0.01, 0.03, 0.05, 0.08, 0.10)
_BANKNIFTY_THRESHOLD_MULTIPLIER = 1.3
_BLOCK_AT_5PCT_GAP_LOSS_PCT = 1.5
_BLOCK_AT_10PCT_GAP_LOSS_PCT = 3.0


@dataclass
class GapScenarioResult:
    gap_pct: float
    direction: str  # "up" | "down"
    pnl: float


def scenario_pnl(
    underlying_price: float,
    gap_pct: float,
    direction: str,
    *,
    net_delta: float,
    net_gamma: float,
    net_vega: float,
    atm_iv: float,
) -> float:
    """P&L for one gap scenario: Delta + Gamma + Vega impact (§11.5).

    Greeks are native units (delta in points, vega in rupees per vol point). The spec's
    "IV spike ~35%" is a RELATIVE rise of current IV, so the vol-point move is
    ``spike x atm_iv x 100`` (e.g. 35% of an 18% IV = 6.3 vol points), not 35 points.
    """
    sign = 1.0 if direction == "up" else -1.0
    move = underlying_price * gap_pct * sign
    iv_spike = _IV_SPIKE_MODEL.get(gap_pct, 0.0)
    delta_pnl = net_delta * move
    gamma_pnl = 0.5 * net_gamma * move * move
    vega_pnl = net_vega * iv_spike * atm_iv * 100.0  # vega is per vol point
    return delta_pnl + gamma_pnl + vega_pnl


def run_gap_stress_test(
    underlying_price: float,
    *,
    net_delta: float,
    net_gamma: float,
    net_vega: float,
    atm_iv: float,
) -> list[GapScenarioResult]:
    return [
        GapScenarioResult(
            gap_pct,
            direction,
            scenario_pnl(
                underlying_price,
                gap_pct,
                direction,
                net_delta=net_delta,
                net_gamma=net_gamma,
                net_vega=net_vega,
                atm_iv=atm_iv,
            ),
        )
        for gap_pct in _GAP_SCENARIOS
        for direction in ("up", "down")
    ]


def blocking_check(
    results: list[GapScenarioResult], options_capital: float, *, is_banknifty: bool = False
) -> tuple[bool, str]:
    """(blocked, reason) — True if the worst 5% or 10% gap loss breaches its
    threshold. BANKNIFTY thresholds are widened 1.3x (its gaps run ~1.3x NIFTY's)."""
    mult = _BANKNIFTY_THRESHOLD_MULTIPLIER if is_banknifty else 1.0
    worst_5pct = min((r.pnl for r in results if r.gap_pct == 0.05), default=0.0)
    worst_10pct = min((r.pnl for r in results if r.gap_pct == 0.10), default=0.0)

    limit_5 = options_capital * _BLOCK_AT_5PCT_GAP_LOSS_PCT / 100.0 * mult
    limit_10 = options_capital * _BLOCK_AT_10PCT_GAP_LOSS_PCT / 100.0 * mult

    if -worst_5pct > limit_5:
        return True, f"5% gap loss {abs(worst_5pct):.0f} > limit {limit_5:.0f}"
    if -worst_10pct > limit_10:
        return True, f"10% gap loss {abs(worst_10pct):.0f} > limit {limit_10:.0f}"
    return False, "within gap-risk limits"
