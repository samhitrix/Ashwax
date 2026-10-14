"""
Layer 7 â€” Causal Trade Memory (spec Â§9).

LLM produces a TradeInsight per closed trade.
Before the next similar trade, agent retrieves 10 most relevant insights as context.
Never used as hard rules â€” context only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from src.infrastructure.logger import get_logger

logger = get_logger(__name__)

_RETRIEVE_K = 10

_PROMPT = """You are a trading post-trade analyst. Return ONLY valid JSON:
{{
  "was_signal_valid": true|false,
  "failure_was_environmental": true|false,
  "what_i_would_do_differently": "...",
  "generalizable_insight": "...",
  "confidence_in_insight": 0.0-1.0,
  "applicable_when": "..."
}}
trade_id:{trade_id} strategy:{strategy} pnl_r:{pnl_r} regime:{regime} sector:{sector}
entry_reasoning:{entry_reasoning}"""


@dataclass
class TradeInsight:
    trade_id: str
    was_signal_valid: bool
    failure_was_environmental: bool
    what_i_would_do_differently: str
    generalizable_insight: str
    confidence_in_insight: float
    applicable_when: str
    regime_at_trade: str
    sector_at_trade: str
    strategy: str = ""


class InsightRepository(Protocol):
    async def save(self, insight: TradeInsight) -> None: ...
    async def find_similar(self, strategy: str, regime: str, k: int) -> list[TradeInsight]: ...


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


class CausalMemory:
    """
    Stores and retrieves causal trade insights.

    Usage:
        memory = CausalMemory(llm, repo)
        insight = await memory.analyse_and_store(trade_id, strategy, pnl_r, regime, sector)
        context = await memory.retrieve(strategy, regime)

    `llm` may be None for a retrieve()-only instance (analyse_and_store() is
    never called on it) — orchestrator.py's signal-build-time retrieval uses
    this to avoid constructing an LLM client just to read existing insights.
    analyse_and_store() already falls back to _fallback() on ANY exception
    from self._llm, so a None client there degrades safely too.
    """

    def __init__(self, llm: LLMClient | None, repo: InsightRepository) -> None:
        self._llm = llm
        self._repo = repo

    async def analyse_and_store(
        self,
        trade_id: str,
        strategy: str,
        pnl_r: float,
        regime: str,
        sector: str,
        entry_reasoning: str = "",
    ) -> TradeInsight:
        prompt = _PROMPT.format(
            trade_id=trade_id,
            strategy=strategy,
            pnl_r=pnl_r,
            regime=regime,
            sector=sector,
            entry_reasoning=entry_reasoning[:400],
        )
        try:
            if self._llm is None:
                raise RuntimeError("causal_memory: no LLM client (retrieve()-only instance)")
            raw = await self._llm.complete(prompt)
            insight = _parse(raw, trade_id, strategy, regime, sector)
        except Exception:
            logger.warning("causal_memory LLM failed trade_id=%s â€” fallback", trade_id)
            insight = _fallback(trade_id, strategy, pnl_r, regime, sector)

        try:
            await self._repo.save(insight)
        except Exception:
            logger.error("causal_memory save failed trade_id=%s", trade_id)

        return insight

    async def retrieve(
        self, strategy: str, regime: str, k: int = _RETRIEVE_K
    ) -> list[TradeInsight]:
        try:
            return await self._repo.find_similar(strategy, regime, k)
        except Exception:
            logger.warning("causal_memory retrieve failed strategy=%s", strategy)
            return []


def _parse(raw: str, trade_id: str, strategy: str, regime: str, sector: str) -> TradeInsight:
    import json

    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    d = json.loads(text)
    return TradeInsight(
        trade_id=trade_id,
        was_signal_valid=bool(d.get("was_signal_valid", True)),
        failure_was_environmental=bool(d.get("failure_was_environmental", False)),
        what_i_would_do_differently=str(d.get("what_i_would_do_differently", ""))[:500],
        generalizable_insight=str(d.get("generalizable_insight", ""))[:500],
        confidence_in_insight=float(max(0.0, min(1.0, d.get("confidence_in_insight", 0.5)))),
        applicable_when=str(d.get("applicable_when", ""))[:200],
        regime_at_trade=regime,
        sector_at_trade=sector,
        strategy=strategy,
    )


def _fallback(trade_id: str, strategy: str, pnl_r: float, regime: str, sector: str) -> TradeInsight:
    return TradeInsight(
        trade_id=trade_id,
        was_signal_valid=pnl_r > 0,
        failure_was_environmental=False,
        what_i_would_do_differently="LLM unavailable â€” manual review needed",
        generalizable_insight=f"Trade {'win' if pnl_r > 0 else 'loss'} in {regime} regime",
        confidence_in_insight=0.3,
        applicable_when=f"Similar {strategy} setups in {regime}",
        regime_at_trade=regime,
        sector_at_trade=sector,
        strategy=strategy,
    )
