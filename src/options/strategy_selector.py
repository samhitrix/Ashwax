"""Seller-desk strategy selection engine (options_spec.md §4).

Given the current regime, IVR, term structure, and event-blackout status,
selects ONE recommended strategy TYPE from the regime-gated menu (§4 table)
and evaluates it against the mandatory EV gate. This stage decides WHAT to
trade; strike/expiry selection (a later build-order stage per §23) decides
the actual legs — this module works from caller-supplied delta/win/loss
estimates, not live strikes.

``protective_put``/``collar`` are intentionally excluded from the menu here:
the spec triggers them from an existing CNC equity position + earnings
proximity, not from regime/IVR — that's a different trigger already handled
by the buyer-first ``src.options.structure_selector``, not duplicated here.
``diagonal_spread`` is excluded too — it needs a directional-bias signal this
selector doesn't model; deferred rather than guessed.

POP (probability of profit) is estimated from short-strike delta — the
standard options approximation (|delta| ≈ risk-neutral probability of
expiring ITM). For multi-leg structures this uses a simple additive breach
model (POP ≈ 1 - Σ|delta_leg|), intentionally MORE conservative than the
spec's own illustrative "~60% POP for a 25-delta Iron Condor" (which implies
a joint-probability correction this module doesn't attempt) — erring
conservative given real capital is at stake.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.options import seller_settings as _seller_settings
from src.options.greeks import DEFAULT_RISK_FREE_RATE

if TYPE_CHECKING:
    from src.options.structure_selector import OptionLeg

# 2026-08-20 calibration against 277 days of real NIFTY history: regime+IVR
# jointly cleared for a credit trade on only 10.8% of days, and 22.0% of all
# days were blocked on IVR alone (regime was fine, IVR<55 wasn't "rich"
# enough for the old all-or-nothing bar). bull_put_spread/bear_call_spread
# already ignore regime (valid_regimes=None) — IVR was the only real gate —
# so rather than forcing a full-size trade into thinner-than-ideal premium,
# this band lets them fire at half size instead of not at all. Iron
# condor/butterfly keep their original 55/70 bar unchanged: this is scoped
# to single-leg-directional credit spreads only, not the regime-gated
# range-bound structures.
#
# These IVR numbers, plus iron_condor/iron_butterfly's floor in _menu()
# below, are read fresh from config (options_settings.json iv_thresholds)
# on every call via _reduced_size_ivr_band()/_menu() -- not frozen at
# import time -- so a config change (or seller_settings.reload()) takes
# effect without a process restart. Previously these disagreed with a
# separate, unused iv_thresholds config block (orchestrator.py's
# _seller_iv_cfg, loaded and never read) -- that dead config is now this
# module's real, single source of truth instead (found+fixed 2026-09-04).
_DEFAULT_IV_THRESHOLDS = {
    "iron_condor_min_ivr": 55.0,
    "iron_butterfly_min_ivr": 70.0,
    "reduced_size_min_ivr": 25.0,
    "reduced_size_max_ivr": 55.0,
}
_REDUCED_SIZE_TYPES = frozenset({"bull_put_spread", "bear_call_spread"})
_REDUCED_SIZE_MULTIPLIER = 0.5


def _reduced_size_ivr_band() -> tuple[float, float]:
    """[lo, hi) = moderate, not rich -- see module comment above."""
    cfg = _seller_settings.iv_thresholds()
    lo = float(cfg.get("reduced_size_min_ivr", _DEFAULT_IV_THRESHOLDS["reduced_size_min_ivr"]))
    hi = float(cfg.get("reduced_size_max_ivr", _DEFAULT_IV_THRESHOLDS["reduced_size_max_ivr"]))
    return lo, hi


# 2026-08-20 calibration (same session): the regime-gap analysis above showed
# the ADX 18-25 / low-VIX zone is UNDEFINED ~44% of NIFTY days — no selling,
# and no directional entry either, since bull_call_spread/bear_put_spread
# were only offered inside a clean LOW_VOL_TRENDING/HIGH_VOL_TRENDING regime
# (ADX>25, R²>0.70). A real (not noise-level) directional drift still exists
# well before ADX clears 25. ``directional_bias`` here must be the SIGNED
# drift (regime_classifier.trend_direction), not trending_score's R²
# (unsigned — can't tell an uptrend from a downtrend). Threshold is the
# ~75th/25th percentile of NIFTY's 20-bar signed drift over 1386 real
# sessions (|slope/mean_price| ~0.0015-0.002) — i.e. only the top/bottom
# quartile of days, not everything with a nonzero wobble.
_TRENDING_REGIMES = frozenset({"LOW_VOL_TRENDING", "HIGH_VOL_TRENDING"})
_DIRECTIONAL_DEBIT_TYPES = frozenset({"bull_call_spread", "bear_put_spread"})
_MODERATE_TREND_BIAS_THRESHOLD = 0.0015


def _low_ivr_candidacy_floor() -> float | None:
    """low_ivr_mode.candidacy_min_ivr -- when the mode is enabled, this
    REPLACES the reduced-size band's floor as the actual candidacy gate for
    bull_put_spread/bear_call_spread, not just the strike/DTE profile
    (structure_builder._low_ivr_profile). Found 2026-09-04 (live, on real
    BANKNIFTY IVR of 4-7%): the delta/DTE tiering in low_ivr_mode was
    unreachable dead code without this, because _menu()'s own floor
    (reduced_size_min_ivr, 25 by default) blocked candidacy before
    low_ivr_mode's ivr_threshold check was ever reached -- a credit spread
    at ivr=5 never became a candidate in the first place, so nothing further
    down the pipeline could apply a tighter profile to it. Returns None if
    low_ivr_mode is disabled (no change to _menu()'s existing floor)."""
    cfg = _seller_settings.low_ivr_mode()
    if not cfg.get("enabled", False):
        return None
    val = cfg.get("candidacy_min_ivr")
    return float(val) if val is not None else None


def _menu() -> list[tuple[str, frozenset[str] | None, float | None, float | None]]:
    """Regime-gated strategy menu (options_spec.md §4 table): (type,
    valid_regimes or None for "any", min_ivr, max_ivr). Rebuilt fresh on
    every call (not a frozen module-level list) so the IVR floors read from
    config stay live -- see module comment above _DEFAULT_IV_THRESHOLDS."""
    cfg = _seller_settings.iv_thresholds()
    ic_min = float(cfg.get("iron_condor_min_ivr", _DEFAULT_IV_THRESHOLDS["iron_condor_min_ivr"]))
    ib_min = float(
        cfg.get("iron_butterfly_min_ivr", _DEFAULT_IV_THRESHOLDS["iron_butterfly_min_ivr"])
    )
    reduced_lo, _reduced_hi = _reduced_size_ivr_band()
    credit_spread_min_ivr = _low_ivr_candidacy_floor()
    if credit_spread_min_ivr is None:
        credit_spread_min_ivr = reduced_lo
    return [
        ("iron_condor", frozenset({"LOW_VOL_RANGE_BOUND", "HIGH_VOL_RANGE_BOUND"}), ic_min, None),
        (
            "iron_butterfly",
            frozenset({"LOW_VOL_RANGE_BOUND", "HIGH_VOL_RANGE_BOUND"}),
            ib_min,
            None,
        ),
        ("bull_put_spread", None, credit_spread_min_ivr, None),
        ("bear_call_spread", None, credit_spread_min_ivr, None),
        ("long_straddle", frozenset({"LOW_VOL_TRENDING", "HIGH_VOL_TRENDING"}), None, 30.0),
        ("long_strangle", frozenset({"LOW_VOL_TRENDING", "HIGH_VOL_TRENDING"}), 25.0, 40.0),
        ("bull_call_spread", frozenset({"LOW_VOL_TRENDING", "HIGH_VOL_TRENDING"}), 30.0, 55.0),
        ("bear_put_spread", frozenset({"LOW_VOL_TRENDING", "HIGH_VOL_TRENDING"}), 30.0, 55.0),
    ]


_DIAGONAL_BIAS_THRESHOLD = 0.15  # starting hypothesis, see design doc Failure Conditions

_CREDIT_TYPES = frozenset({"iron_condor", "iron_butterfly", "bull_put_spread", "bear_call_spread"})
# Debit-only structures survive an active selling blackout — the block is on
# collecting premium, not on defined-risk directional buying.
_DEBIT_TYPES = frozenset({"long_straddle", "long_strangle", "bull_call_spread", "bear_put_spread"})


def sizing_multiplier(strategy_type: str, ivr: float | None, *, regime: str | None = None) -> float:
    """Position-size scale-down for a trade entered on a weaker-than-ideal
    signal: a credit spread at moderate (not rich) IVR, a credit spread even
    deeper below that (low_ivr_mode's candidacy floor -- "scale down, don't
    force it" per the low-IV playbook this mode implements), or a
    directional debit spread entered via the moderate-trend override (real
    regime.py quality confirmation) instead of a clean TRENDING regime. 1.0
    (full size) everywhere else."""
    if strategy_type in _REDUCED_SIZE_TYPES and ivr is not None:
        lo, hi = _reduced_size_ivr_band()
        if lo <= ivr < hi:
            return _REDUCED_SIZE_MULTIPLIER
        low_ivr_floor = _low_ivr_candidacy_floor()
        if low_ivr_floor is not None and low_ivr_floor <= ivr < lo:
            cfg = _seller_settings.low_ivr_mode()
            return float(cfg.get("candidacy_size_multiplier", _REDUCED_SIZE_MULTIPLIER))
    if strategy_type in _DIRECTIONAL_DEBIT_TYPES and regime not in _TRENDING_REGIMES:
        return _REDUCED_SIZE_MULTIPLIER
    return 1.0


@dataclass
class StrategyRecommendation:
    strategy_type: str
    direction: str  # "credit" | "debit"
    reasoning: str
    estimated_pop: float
    expected_value: float


def is_debit_type(strategy_type: str) -> bool:
    """True for structures that survive an active selling blackout (the
    block is on collecting premium, not defined-risk directional buying)."""
    return strategy_type in _DEBIT_TYPES


def estimate_pop(short_deltas: list[float]) -> float:
    """POP from short-strike delta(s) — see module docstring for the model."""
    breach_prob = sum(abs(d) for d in short_deltas)
    return max(0.0, 1.0 - breach_prob)


def debit_vertical_pop(
    strategy_type: str,
    protection_legs: list[OptionLeg],
    income_legs: list[OptionLeg],
    *,
    spot: float,
    iv: float | None,
    dte: int,
    r: float = DEFAULT_RISK_FREE_RATE,
) -> float | None:
    """Probability of profit at expiry for a directional DEBIT vertical, or ``None`` when
    ``strategy_type`` is not one (caller then uses the credit-spread proxy).

    A debit spread wins only if price finishes beyond BREAKEVEN (long strike +/- net debit),
    NOT merely short of the short strike, so ``1 - |short delta|`` (right for a credit
    spread) badly overstates it: a BANKNIFTY bear_put_spread was logged pop=0.746 / EV=+10.6k
    when the real probability was ~0.43 (2026-09-28). Lognormal, risk-neutral drift.
    Fails safe to 0.0 (EV gate rejects) if IV or time is unusable.
    """
    if strategy_type not in _DIRECTIONAL_DEBIT_TYPES:
        return None
    long_leg = next((leg for leg in protection_legs + income_legs if leg.direction == "BUY"), None)
    short_leg = next(
        (leg for leg in protection_legs + income_legs if leg.direction == "SELL"), None
    )
    if long_leg is None or short_leg is None or iv is None or iv <= 0.0 or dte <= 0 or spot <= 0.0:
        return 0.0
    debit = long_leg.premium - short_leg.premium
    if debit <= 0.0:
        return 0.0
    bullish = strategy_type == "bull_call_spread"
    breakeven = long_leg.strike + debit if bullish else long_leg.strike - debit
    if breakeven <= 0.0:
        return 0.0
    t = dte / 365.0
    d2 = (math.log(spot / breakeven) + (r - iv * iv / 2.0) * t) / (iv * math.sqrt(t))
    z = d2 if bullish else -d2
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


_POP_CALIBRATED_STRATEGIES = frozenset({"bull_put_spread", "bear_call_spread"})


def calibrated_pop(strategy_type: str, short_deltas: list[float]) -> float:
    """POP for the EV gate. Falls back to the crude estimate_pop() touch-
    probability proxy unless config's pop_calibration is enabled AND
    strategy_type is one of the two types scripts/backtest_options_seller_regime.py
    has actually backtested (bull_put_spread/bear_call_spread).

    The crude proxy's implicit intercept -- 100% win rate at 0 short delta --
    is provably wrong: a no-lookahead walk-forward backtest (2026-09-08,
    n=1300 single-sided credit-spread trials per underlying, BANKNIFTY+NIFTY,
    2020-2026) measured real win rates of ~77.5% at the 0.25 default delta and
    ~71.6% at low_ivr_mode's 0.35 delta -- both well above what
    pop=1-delta would predict (0.75/0.65), which is why the EV gate
    (avg_win=0.5x premium, avg_loss=1.5x premium, algebraically requiring
    pop>0.75) was silently rejecting every real candidate. This linearly
    interpolates/extrapolates between those two measured anchor points
    instead of assuming a slope of exactly 1.0."""
    cfg = _seller_settings.pop_calibration()
    if not cfg.get("enabled", False) or strategy_type not in _POP_CALIBRATED_STRATEGIES:
        return estimate_pop(short_deltas)
    try:
        x0, y0 = float(cfg["anchor_low_delta"]), float(cfg["anchor_low_delta_pop"])
        x1, y1 = float(cfg["anchor_high_delta"]), float(cfg["anchor_high_delta_pop"])
    except (KeyError, TypeError, ValueError):
        return estimate_pop(short_deltas)
    if x1 == x0:
        return estimate_pop(short_deltas)
    delta_sum = sum(abs(d) for d in short_deltas)
    slope = (y1 - y0) / (x1 - x0)
    pop = y0 + slope * (delta_sum - x0)
    return max(0.0, min(1.0, pop))


def compute_ev(pop: float, avg_win: float, avg_loss: float) -> float:
    """Mandatory EV gate (options_spec.md §4). Trade rejected if EV <= 0."""
    return pop * avg_win - (1.0 - pop) * avg_loss


class StrategySelector:
    def candidates(
        self,
        regime: str,
        ivr: float | None,
        *,
        term_structure: str | None = None,
        directional_bias: float | None = None,
    ) -> list[str]:
        """Strategy types the regime+IVR combination allows — menu match only,
        no EV gate or leg construction yet."""
        if ivr is None:
            return []
        out = [
            strategy_type
            for strategy_type, valid_regimes, min_ivr, max_ivr in _menu()
            if (valid_regimes is None or regime in valid_regimes)
            and (min_ivr is None or ivr >= min_ivr)
            and (max_ivr is None or ivr <= max_ivr)
        ]
        # Moderate-trend directional override: outside a clean TRENDING
        # regime, a real (not noise-level) signed drift still justifies a
        # smaller defined-risk directional bet, reduced size applied by the
        # caller via sizing_multiplier(). directional_bias must be SIGNED
        # (regime_classifier.trend_direction) here, not trending_score's R².
        if (
            regime not in _TRENDING_REGIMES
            and directional_bias is not None
            and abs(directional_bias) >= _MODERATE_TREND_BIAS_THRESHOLD
        ):
            debit_type = "bull_call_spread" if directional_bias > 0 else "bear_put_spread"
            _, _, min_ivr, max_ivr = next(m for m in _menu() if m[0] == debit_type)
            if (
                debit_type not in out
                and (min_ivr is None or ivr >= min_ivr)
                and (max_ivr is None or ivr <= max_ivr)
            ):
                out.append(debit_type)
        if (
            term_structure == "backwardation"
            and regime in {"LOW_VOL_RANGE_BOUND", "HIGH_VOL_RANGE_BOUND"}
            and "calendar_spread" not in out
        ):
            out.append("calendar_spread")
        if (
            term_structure == "backwardation"
            and regime in {"LOW_VOL_RANGE_BOUND", "HIGH_VOL_RANGE_BOUND"}
            and directional_bias is not None
            and abs(directional_bias) >= _DIAGONAL_BIAS_THRESHOLD
            and "diagonal_spread" not in out
        ):
            out.append("diagonal_spread")
        return out

    def select(
        self,
        regime: str,
        ivr: float | None,
        *,
        term_structure: str | None,
        short_deltas_by_strategy: dict[str, list[float]],
        avg_win_by_strategy: dict[str, float],
        avg_loss_by_strategy: dict[str, float],
        selling_blocked: bool,
    ) -> StrategyRecommendation | None:
        """Best EV-positive candidate, or None if nothing qualifies."""
        candidates = self.candidates(regime, ivr, term_structure=term_structure)
        if selling_blocked:
            candidates = [c for c in candidates if c in _DEBIT_TYPES]

        best: StrategyRecommendation | None = None
        for strategy_type in candidates:
            deltas = short_deltas_by_strategy.get(strategy_type)
            avg_win = avg_win_by_strategy.get(strategy_type)
            avg_loss = avg_loss_by_strategy.get(strategy_type)
            if deltas is None or avg_win is None or avg_loss is None:
                continue
            pop = estimate_pop(deltas)
            ev = compute_ev(pop, avg_win, avg_loss)
            if ev <= 0.0:
                continue
            if best is None or ev > best.expected_value:
                direction = "credit" if strategy_type in _CREDIT_TYPES else "debit"
                best = StrategyRecommendation(
                    strategy_type=strategy_type,
                    direction=direction,
                    reasoning=f"regime={regime} ivr={ivr:.0f} pop={pop:.2f} ev={ev:.0f}",
                    estimated_pop=pop,
                    expected_value=ev,
                )
        return best
