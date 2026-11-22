"""Setup matcher — matches live fingerprints against historical outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from src.strategies.backtest.fingerprinter import FingerprintHasher
from src.strategies.backtest.pre_trade import SetupFingerprint


@dataclass
class MatchResult:
    hash_key: str
    matched_count: int
    win_rate: float
    avg_expectancy_r: float
    sufficient_data: bool


class SetupMatcher:
    MIN_MATCHES = 10

    def __init__(self) -> None:
        self._hasher = FingerprintHasher()
        self._history: dict[str, list[float]] = {}

    def record_outcome(self, fp: SetupFingerprint, expectancy_r: float) -> None:
        self._history.setdefault(self._hasher.hash(fp), []).append(expectancy_r)

    def match(self, fp: SetupFingerprint) -> MatchResult:
        h = self._hasher.hash(fp)
        outcomes = self._history.get(h, [])
        if not outcomes:
            return MatchResult(h, 0, 0.0, 0.0, False)
        wins = sum(1 for r in outcomes if r > 0)
        return MatchResult(
            h,
            len(outcomes),
            wins / len(outcomes),
            sum(outcomes) / len(outcomes),
            len(outcomes) >= self.MIN_MATCHES,
        )
