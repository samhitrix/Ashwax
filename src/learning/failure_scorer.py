"""Layer 4 — Failure Scorer."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger(__name__)  # "src.learning.failure_scorer" -> learning.log


@dataclass
class FailureMode:
    mode: str
    count: int = 0
    avg_loss_r: float = 0.0


@dataclass
class FailureScoreReport:
    strategy_name: str
    dominant_failure: str
    failure_modes: list[FailureMode] = field(default_factory=list)


class FailureScorer:
    def score(self, strategy_name: str, trades: list[dict[str, Any]]) -> FailureScoreReport:
        losses = [t for t in trades if t.get("pnl_r", 0) < 0]
        counts: dict[str, list[float]] = {}
        for t in losses:
            counts.setdefault(t.get("attribution", "UNKNOWN"), []).append(abs(t.get("pnl_r", 0)))
        modes = [
            FailureMode(m, len(v), sum(v) / len(v))
            for m, v in sorted(counts.items(), key=lambda x: len(x[1]), reverse=True)
        ]
        return FailureScoreReport(strategy_name, modes[0].mode if modes else "NONE", modes)


# Small, explicit, human-reviewed mapping — per this project's "no speculative
# abstraction" rule, only mapped where there's a real, defensible causal link
# between the failure mode and a mutable SL/target param. The mutation engine
# (src/strategies/evolution.py) only ever mutates sl_*/target_* fields, so
# REGIME_MISMATCH/SIGNAL_QUALITY/NEWS_ADVERSE (entry-condition/setup-quality
# issues, not stop-mechanics issues) have no honest param correlate here and
# are deliberately left unmapped — evolve() falls back to uniform selection for
# those, rather than a fabricated mapping.
_FAILURE_MODE_PARAM_BIAS: dict[str, tuple[str, ...]] = {
    "TIMING_FAILURE": ("sl_atr_multiplier", "sl_buffer_pct", "sl_lookback_days"),
}
_BIASED_PARAM_WEIGHT = 5.0


def param_bias_for(dominant_failure: str, param_keys: list[str]) -> dict[str, float] | None:
    """Mutation-selection weights (src.strategies.evolution.evolve's param_bias)
    for a given dominant failure mode, restricted to params this strategy
    actually has. None (uniform selection, unchanged behavior) when the
    failure mode has no mapped param or none of the mapped params apply."""
    mapped = _FAILURE_MODE_PARAM_BIAS.get(dominant_failure)
    if not mapped:
        return None
    applicable = [p for p in mapped if p in param_keys]
    if not applicable:
        return None
    return {p: (_BIASED_PARAM_WEIGHT if p in applicable else 1.0) for p in param_keys}
