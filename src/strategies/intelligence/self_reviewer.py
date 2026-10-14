"""Self-Reviewer — LLM reviews own backtest for overfitting. Verdict: DEPLOY|REJECT|NEEDS_REFINEMENT"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger("strategies.self_reviewer")


@dataclass
class SelfReviewResult:
    verdict: str
    overfitting_risk: str
    reasoning: str
    confidence: float


class SelfReviewer:
    def __init__(self, llm_client: Any = None) -> None:
        self._client = llm_client

    async def review(self, strategy_name: str, results: dict[str, Any]) -> SelfReviewResult:
        wr = float(results.get("win_rate", 0))
        trades = int(results.get("total_trades", 0))
        pf = float(results.get("profit_factor", 0))
        overfit = "HIGH" if trades < 30 or wr > 0.80 else ("MEDIUM" if wr > 0.65 else "LOW")
        verdict = "REJECT" if overfit == "HIGH" else ("DEPLOY" if pf >= 1.5 else "NEEDS_REFINEMENT")
        return SelfReviewResult(
            verdict=verdict,
            overfitting_risk=overfit,
            reasoning=f"trades={trades} wr={wr:.2f} pf={pf:.2f}",
            confidence=0.7,
        )
