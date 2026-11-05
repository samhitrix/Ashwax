"""
Correlation Crisis Detector — CRM G7 sub-check.

During high-volatility regimes, multi-stock portfolios can become highly
correlated (all stocks move together), invalidating the diversification
assumption.  When the average pairwise correlation of open positions exceeds a
threshold the system is in a correlation crisis and should stop adding new
positions.

Input:  dict[symbol -> list[float]] of recent returns (e.g. 20 daily returns)
Output: CorrelationStatus dataclass

Algorithm:
  1. Compute pairwise Pearson correlations for all symbol-pairs.
  2. Average the upper-triangle (excluding diagonal).
  3. Compare against WARN / CRISIS thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class CorrelationLevel(StrEnum):
    NORMAL = "NORMAL"  # avg correlation < warn threshold
    WARN = "WARN"  # avg correlation >= warn threshold
    CRISIS = "CRISIS"  # avg correlation >= crisis threshold


_WARN_THRESHOLD = 0.70
_CRISIS_THRESHOLD = 0.85
_MIN_PERIODS = 10  # minimum return observations needed for a valid reading


@dataclass
class CorrelationStatus:
    level: CorrelationLevel
    avg_correlation: float
    max_pair_correlation: float
    max_pair: tuple[str, str]
    reason: str
    symbol_count: int


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation coefficient; returns 0.0 if underdetermined."""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    mean_a = sum(a[:n]) / n
    mean_b = sum(b[:n]) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    std_a = math.sqrt(sum((x - mean_a) ** 2 for x in a[:n]))
    std_b = math.sqrt(sum((x - mean_b) ** 2 for x in b[:n]))
    if std_a == 0 or std_b == 0:
        return 0.0
    return cov / (std_a * std_b)


def detect_correlation_crisis(
    returns: dict[str, list[float]],
) -> CorrelationStatus:
    """
    Detect whether open positions are in a correlation crisis.

    Args:
        returns: Map of symbol -> list of recent daily returns (floats, e.g. 0.012).
                 All series should ideally have the same length.

    Returns:
        CorrelationStatus with level, avg, and max pair info.
    """
    symbols = [s for s, r in returns.items() if len(r) >= _MIN_PERIODS]

    if len(symbols) < 2:
        return CorrelationStatus(
            level=CorrelationLevel.NORMAL,
            avg_correlation=0.0,
            max_pair_correlation=0.0,
            max_pair=("", ""),
            reason=f"Fewer than 2 symbols with >= {_MIN_PERIODS} observations",
            symbol_count=len(symbols),
        )

    pairs: list[tuple[str, str, float]] = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            s1, s2 = symbols[i], symbols[j]
            corr = _pearson(returns[s1], returns[s2])
            pairs.append((s1, s2, corr))

    correlations = [c for _, _, c in pairs]
    avg_corr = sum(correlations) / len(correlations)
    max_corr = max(correlations)
    max_pair_entry = max(pairs, key=lambda t: t[2])
    max_pair = (max_pair_entry[0], max_pair_entry[1])

    if avg_corr >= _CRISIS_THRESHOLD:
        level = CorrelationLevel.CRISIS
        reason = (
            f"Avg pairwise correlation {avg_corr:.2f} >= crisis threshold "
            f"{_CRISIS_THRESHOLD:.2f} — stop new entries"
        )
    elif avg_corr >= _WARN_THRESHOLD:
        level = CorrelationLevel.WARN
        reason = (
            f"Avg pairwise correlation {avg_corr:.2f} >= warn threshold "
            f"{_WARN_THRESHOLD:.2f} — reduce position sizing"
        )
    else:
        level = CorrelationLevel.NORMAL
        reason = f"Avg pairwise correlation {avg_corr:.2f} — normal"

    return CorrelationStatus(
        level=level,
        avg_correlation=round(avg_corr, 4),
        max_pair_correlation=round(max_corr, 4),
        max_pair=max_pair,
        reason=reason,
        symbol_count=len(symbols),
    )
