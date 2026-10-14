"""Opportunity Value Model — anti-FOMO gate per docs/04-agent-core.md §10."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)  # "src.intelligence.opportunity_model" -> intelligence.log


@dataclass
class OpportunityScore:
    signal_id: str
    symbol: str
    base_score: float
    adjusted_score: float
    fomo_penalty: float
    should_take: bool
    reason: str


class OpportunityValueModel:
    MIN_SCORE = 65.0
    FOMO_THRESHOLD_PCT = 0.02

    def evaluate(
        self, signal_id: str, symbol: str, base_score: float, context: dict[str, Any]
    ) -> OpportunityScore:
        move = abs(float(context.get("price_move_since_signal", 0)))
        fomo = min(20.0, move * 500) if move > self.FOMO_THRESHOLD_PCT else 0.0
        mult = {"BULL": 1.0, "SIDEWAYS": 0.8, "BEAR": 0.5, "UNKNOWN": 0.3}.get(
            context.get("regime", "UNKNOWN"), 0.7
        )
        adjusted = (base_score - fomo) * mult
        take = adjusted >= self.MIN_SCORE
        return OpportunityScore(
            signal_id=signal_id,
            symbol=symbol,
            base_score=base_score,
            adjusted_score=adjusted,
            fomo_penalty=fomo,
            should_take=take,
            reason="TAKE" if take else f"SKIP adj={adjusted:.1f}",
        )
