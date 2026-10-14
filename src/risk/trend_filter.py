"""Per-stock daily-trend caution for INTRADAY entries.

Regime is a swing-trade concern; intraday cares about the *stock's own* trend.
A long breakout on a name sitting below both its 50- and 200-day moving averages
(a confirmed daily downtrend) is lower-probability — so we trade it cautiously
(smaller size), not blocked. Symmetric for shorts above both DMAs.

This is deliberately soft (size multiplier, fail-open) rather than a hard rule.
"""

from __future__ import annotations

CAUTIOUS_MULT = 0.5


def daily_trend_size_multiplier(
    close: float,
    sma50: float | None,
    sma200: float | None,
    direction: str,
    cautious_mult: float = CAUTIOUS_MULT,
) -> float:
    """Return a size multiplier for an intraday signal vs the stock's daily trend.

    * BUY  while close < 50DMA and < 200DMA  → cautious (fighting a downtrend).
    * SELL while close > 50DMA and > 200DMA  → cautious (fighting an uptrend).
    * Otherwise, or when DMAs are unavailable → 1.0 (full size, never blocks).
    """
    if not sma50 or not sma200 or close <= 0:
        return 1.0
    d = direction.upper()
    if d == "BUY" and close < sma50 and close < sma200:
        return cautious_mult
    if d == "SELL" and close > sma50 and close > sma200:
        return cautious_mult
    return 1.0
