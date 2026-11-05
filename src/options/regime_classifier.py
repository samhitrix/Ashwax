"""Four-regime market classifier for the seller-first engine (options_spec.md §3).

Every market state is classified into exactly one of the spec's four regimes,
plus an honest fifth fallback (``UNDEFINED``) for VIX/ADX bands the spec's
four regimes don't cover (e.g. VIX 15-25, ADX 18-25) — treated conservatively
(no selling, no directional) since it isn't one of the spec's validated
environments, not silently forced into the nearest one.

``trending_score``/``mean_revert_score`` are referenced by the spec without a
formula. This uses the standard quant proxy — R² of a linear-regression fit
of price vs. time (high R² = trending, low R² = choppy) — documented here as
a deliberate choice, not a spec-given exact formula.
"""

from __future__ import annotations

from dataclasses import dataclass

_ADX_PERIOD = 14
_TREND_LOOKBACK = 20


@dataclass
class RegimeResult:
    regime: str
    allowed_selling: bool
    allowed_directional: bool
    sizing_pct: float
    reasoning: str


def wilder_adx(
    high: list[float], low: list[float], close: list[float], period: int = _ADX_PERIOD
) -> float | None:
    """Standard Wilder's Average Directional Index. ``None`` if insufficient bars."""
    n = len(close)
    if n < period * 2 + 1 or len(high) != n or len(low) != n:
        return None

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    def wilder_smooth(values: list[float]) -> list[float]:
        smoothed = [0.0] * n
        smoothed[period] = sum(values[1 : period + 1])
        for i in range(period + 1, n):
            smoothed[i] = smoothed[i - 1] - (smoothed[i - 1] / period) + values[i]
        return smoothed

    tr_s = wilder_smooth(tr)
    plus_dm_s = wilder_smooth(plus_dm)
    minus_dm_s = wilder_smooth(minus_dm)

    dx = [0.0] * n
    for i in range(period, n):
        if tr_s[i] == 0:
            continue
        plus_di = 100.0 * plus_dm_s[i] / tr_s[i]
        minus_di = 100.0 * minus_dm_s[i] / tr_s[i]
        denom = plus_di + minus_di
        dx[i] = 100.0 * abs(plus_di - minus_di) / denom if denom > 0 else 0.0

    first_adx_idx = period * 2
    if first_adx_idx >= n:
        return None
    adx = sum(dx[period + 1 : first_adx_idx + 1]) / period
    for i in range(first_adx_idx + 1, n):
        adx = (adx * (period - 1) + dx[i]) / period
    return adx


def trending_score(closes: list[float], lookback: int = _TREND_LOOKBACK) -> float | None:
    """R² of a linear-regression fit of price vs. time over the trailing window."""
    if len(closes) < lookback:
        return None
    tail = closes[-lookback:]
    n = len(tail)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(tail) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, tail, strict=False))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x
    predicted = [slope * x + intercept for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(tail, predicted, strict=False))
    ss_tot = sum((y - mean_y) ** 2 for y in tail)
    if ss_tot == 0:
        return 1.0
    return max(0.0, 1.0 - ss_res / ss_tot)


def trend_direction(closes: list[float], lookback: int = _TREND_LOOKBACK) -> float | None:
    """Signed fractional price drift per bar (regression slope / mean price).

    ``trending_score`` (R²) only measures fit *quality*, not direction — it's
    the same value for a clean uptrend and a clean downtrend. This is the
    slope's sign, for callers that need to know which way the drift points
    (e.g. picking bull_call_spread vs bear_put_spread — see
    strategy_selector.py's moderate-trend directional override).
    """
    if len(closes) < lookback:
        return None
    tail = closes[-lookback:]
    n = len(tail)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(tail) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, tail, strict=False))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0 or mean_y == 0:
        return 0.0
    slope = ss_xy / ss_xx
    return slope / mean_y


def mean_revert_score(trend_score: float) -> float:
    """Complement of trending_score — a documented proxy, not an independent measure."""
    return 1.0 - trend_score


def classify_regime(
    vix: float, adx: float, trend_score: float, revert_score: float
) -> RegimeResult:
    """Classify into one of the spec's 4 regimes (options_spec.md §3), or UNDEFINED."""
    if vix < 15 and adx > 25 and trend_score > 0.70:
        return RegimeResult(
            "LOW_VOL_TRENDING",
            allowed_selling=False,
            allowed_directional=True,
            sizing_pct=100.0,
            reasoning=(
                f"vix={vix:.1f}<15, adx={adx:.1f}>25, trend={trend_score:.2f}>0.70 "
                "— cheap premium, clear direction: buy with the trend"
            ),
        )
    if vix < 15 and adx < 18 and revert_score > 0.65:
        return RegimeResult(
            "LOW_VOL_RANGE_BOUND",
            allowed_selling=True,
            allowed_directional=False,
            sizing_pct=100.0,
            reasoning=(
                f"vix={vix:.1f}<15, adx={adx:.1f}<18, revert={revert_score:.2f}>0.65 "
                "— ideal selling environment, buy wings (cheap OTM protection)"
            ),
        )
    if vix > 25 and adx > 25:
        return RegimeResult(
            "HIGH_VOL_TRENDING",
            allowed_selling=False,
            allowed_directional=True,
            sizing_pct=40.0,
            reasoning=f"vix={vix:.1f}>25, adx={adx:.1f}>25 — crisis trend, directional-with-trend only",
        )
    if vix > 25 and adx < 20:
        return RegimeResult(
            "HIGH_VOL_RANGE_BOUND",
            allowed_selling=True,
            allowed_directional=False,
            sizing_pct=112.5,
            reasoning=f"vix={vix:.1f}>25, adx={adx:.1f}<20 — institutional sweet spot, max vol premium",
        )
    return RegimeResult(
        "UNDEFINED",
        allowed_selling=False,
        allowed_directional=False,
        sizing_pct=0.0,
        reasoning=(
            f"vix={vix:.1f} adx={adx:.1f} outside all 4 defined regime bands "
            "— conservative no-trade fallback, not one of the spec's validated environments"
        ),
    )
