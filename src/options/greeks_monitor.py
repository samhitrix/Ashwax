"""Seller-desk portfolio Greeks monitor (options_spec.md §8).

Aggregates per-leg Greeks into rupee-scaled portfolio totals, raises
advisories when budget utilization crosses 80%, and computes daily Greeks
P&L attribution.

Auto-hedging via futures (spec's "if futures enabled" branch) is NOT
implemented — there is no futures order-placement capability anywhere in
the broker layer yet; this module only ever recommends, never places a
futures hedge order. Reducing the highest-gamma-contributor position on a
gamma breach is likewise a recommendation (returns which position), not an
automatic close — closing is position_manager's job, not duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.options.greeks_budget import PortfolioGreeks

_ADVISORY_THRESHOLD_PCT = 0.80


@dataclass
class LegGreekInputs:
    position_id: str
    delta: float
    gamma: float
    theta: float
    vega: float
    qty: int
    lot_size: int


@dataclass
class PortfolioGreeksRs:
    net_delta_rs: float
    net_gamma_rs: float
    net_theta_rs: float
    net_vega_rs: float


@dataclass
class GreeksAdvisory:
    level: str  # "OK" | "ADVISORY" | "BREACH"
    dimension: str
    message: str
    largest_contributor_position_id: str | None = None


def aggregate_portfolio_greeks(
    legs: list[LegGreekInputs], underlying_price: float
) -> PortfolioGreeksRs:
    net_delta = sum(leg.delta * leg.qty * leg.lot_size * underlying_price / 100.0 for leg in legs)
    net_gamma = sum(
        leg.gamma * leg.qty * leg.lot_size * underlying_price**2 / 100.0 for leg in legs
    )
    net_theta = sum(leg.theta * leg.qty * leg.lot_size for leg in legs)
    net_vega = sum(leg.vega * leg.qty * leg.lot_size for leg in legs)
    return PortfolioGreeksRs(net_delta, net_gamma, net_theta, net_vega)


def check_delta_budget(portfolio: PortfolioGreeksRs, delta_budget: float) -> GreeksAdvisory:
    exposure = abs(portfolio.net_delta_rs)
    if exposure > delta_budget:
        return GreeksAdvisory(
            "BREACH",
            "delta",
            f"delta {exposure:.0f} > budget {delta_budget:.0f} — auto-hedge via futures "
            "recommended (not auto-executed, no futures order path exists yet)",
        )
    if exposure > delta_budget * _ADVISORY_THRESHOLD_PCT:
        return GreeksAdvisory(
            "ADVISORY",
            "delta",
            f"delta {exposure:.0f} at {exposure / delta_budget * 100:.0f}% of budget "
            "— consider delta-neutral adjustment",
        )
    return GreeksAdvisory("OK", "delta", "within budget")


def check_vega_budget(
    portfolio: PortfolioGreeksRs, vega_budget: float, legs: list[LegGreekInputs]
) -> GreeksAdvisory:
    exposure = abs(portfolio.net_vega_rs)
    if exposure > vega_budget * _ADVISORY_THRESHOLD_PCT:
        contributor = max(
            legs, key=lambda leg: abs(leg.vega * leg.qty * leg.lot_size), default=None
        )
        level = "BREACH" if exposure > vega_budget else "ADVISORY"
        return GreeksAdvisory(
            level,
            "vega",
            f"vega {exposure:.0f} at {exposure / vega_budget * 100:.0f}% of budget "
            "— close largest vega contributor",
            contributor.position_id if contributor is not None else None,
        )
    return GreeksAdvisory("OK", "vega", "within budget")


def check_gamma_budget(
    portfolio: PortfolioGreeksRs, gamma_budget: float, legs: list[LegGreekInputs]
) -> GreeksAdvisory:
    exposure = abs(portfolio.net_gamma_rs)
    if exposure > gamma_budget:
        contributor = max(
            legs, key=lambda leg: abs(leg.gamma * leg.qty * leg.lot_size), default=None
        )
        return GreeksAdvisory(
            "BREACH",
            "gamma",
            f"gamma {exposure:.0f} > budget {gamma_budget:.0f} — reduce highest-gamma position",
            contributor.position_id if contributor is not None else None,
        )
    return GreeksAdvisory("OK", "gamma", "within budget")


@dataclass
class PnLAttribution:
    delta_pnl: float
    gamma_pnl: float
    theta_pnl: float
    vega_pnl: float

    @property
    def total(self) -> float:
        return self.delta_pnl + self.gamma_pnl + self.theta_pnl + self.vega_pnl


def pnl_attribution_native(
    native: PortfolioGreeks, *, spot_move: float, iv_change_points: float, days: float
) -> PnLAttribution:
    """Greeks P&L attribution in consistent native units (the correct form of section 8).

    ``native`` = summed ``sign x bs_greeks x lot_size x lots`` (delta in points, gamma in
    points^-1, theta in rupees/day, vega in rupees per vol point); ``spot_move`` is the
    underlying move in POINTS, ``iv_change_points`` the change in implied vol in vol
    points, ``days`` the calendar days elapsed. The spec rupee-scaled delta/gamma
    (x spot/100, x spot^2/100) cannot be multiplied by a move in points without a units
    error, so attribution is done on the native Greeks instead.
    """
    return PnLAttribution(
        delta_pnl=native.net_delta * spot_move,
        gamma_pnl=0.5 * native.net_gamma * spot_move**2,
        theta_pnl=native.net_theta * days,
        vega_pnl=native.net_vega * iv_change_points,
    )


def daily_pnl_attribution(
    portfolio: PortfolioGreeksRs, underlying_daily_move: float, daily_iv_change: float
) -> PnLAttribution:
    """§8 Daily Greeks P&L attribution formulas (spec-literal; see
    ``pnl_attribution_native`` for the unit-consistent form the desk uses)."""
    return PnLAttribution(
        delta_pnl=portfolio.net_delta_rs * underlying_daily_move,
        gamma_pnl=0.5 * portfolio.net_gamma_rs * underlying_daily_move**2,
        theta_pnl=portfolio.net_theta_rs * 1.0,
        vega_pnl=portfolio.net_vega_rs * daily_iv_change,
    )
