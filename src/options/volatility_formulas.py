"""Seller-desk volatility formulas (options_spec.md §2). Pure functions, no I/O.

IV Percentile (IVP) is intentionally NOT reimplemented here — it is the exact
same formula as the buyer-first engine's ``iv_percentile`` in
``src.options.nfo_chain``; callers should import that directly.
"""

from __future__ import annotations

import math

# Term-structure classification thresholds (options_spec.md §2).
_FLAT_BAND = 0.01
_BACKWARDATION_THRESHOLD = 0.03


def iv_rank(current_iv: float, history: list[float]) -> float | None:
    """IV Rank: where current IV sits within its own historical range (0-100).

    ``None`` when there's no history or the range is degenerate (max == min) —
    a fail-safe rather than a divide-by-zero.
    """
    if not history:
        return None
    lo, hi = min(history), max(history)
    if hi == lo:
        return None
    return (current_iv - lo) / (hi - lo) * 100.0


def realized_vol(closes: list[float], window: int) -> float | None:
    """Annualized realized volatility from trailing daily closes.

    Sample stdev of log returns over the trailing ``window`` closes,
    annualized by sqrt(252). Requires ``window + 1`` closes (returns need one
    extra price); ``None`` if insufficient data.
    """
    if len(closes) < window + 1:
        return None
    tail = closes[-(window + 1) :]
    log_returns = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    mean = sum(log_returns) / len(log_returns)
    variance = (
        sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        if len(log_returns) > 1
        else 0.0
    )
    return math.sqrt(variance) * math.sqrt(252)


def vol_premium(current_iv: float, realized_vol_30d: float) -> float:
    """IV - RV30. Positive = selling advantageous; negative = buying advantageous."""
    return current_iv - realized_vol_30d


def classify_term_structure(iv_by_dte: dict[int, float]) -> str | None:
    """Classify the vol term structure from nearest vs farthest available DTE bucket.

    contango: near IV < far IV (normal). backwardation: near IV exceeds far IV
    by more than 3% (event risk priced into the front). flat: within 1%.
    ``None`` if fewer than 2 DTE buckets are available.
    """
    if len(iv_by_dte) < 2:
        return None
    near_dte = min(iv_by_dte)
    far_dte = max(iv_by_dte)
    diff = iv_by_dte[near_dte] - iv_by_dte[far_dte]
    if abs(diff) < _FLAT_BAND:
        return "flat"
    if diff > _BACKWARDATION_THRESHOLD:
        return "backwardation"
    return "contango"


def skew_25delta(call_iv_25d: float, put_iv_25d: float) -> float:
    """25-delta put-call skew. High positive = market paying up for downside protection."""
    return put_iv_25d - call_iv_25d


def dealer_gex(
    strikes: list[tuple[float, float, float, float]], spot: float, lot_size: int
) -> float:
    """Aggregate dealer gamma exposure (GEX) — standard public retail-data proxy.

    ``strikes`` is a list of ``(call_gamma, call_oi, put_gamma, put_oi)`` per
    strike. This is the commonly published approximation (assumes dealers are
    net long calls / short puts) — true dealer positioning isn't observable
    from retail market data, so this is a proxy, not ground truth. Positive =
    dealers net long gamma (pinning); negative = net short gamma (trending).
    """
    net_gamma_oi = sum(
        call_gamma * call_oi - put_gamma * put_oi
        for call_gamma, call_oi, put_gamma, put_oi in strikes
    )
    return net_gamma_oi * spot**2 * 0.01 * lot_size
