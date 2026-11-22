"""LLM Market Scanner — proactive market condition scan."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from src.llm.providers import LLMClient

_LOG = logging.getLogger("llm.market_scanner")


@dataclass
class MarketScanResult:
    regime_assessment: str
    top_opportunities: list[str]
    risks_to_watch: list[str]
    confidence: float


class LLMMarketScanner:
    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    async def scan(self, ctx: dict[str, Any]) -> MarketScanResult:
        prompt = (
            f"Analyse: regime={ctx.get('regime')} vix={ctx.get('vix')} "
            f"nifty={ctx.get('nifty_ltp')}\n"
            f'JSON: {{"regime_assessment":str,"top_opportunities":[str],"risks":[str],"confidence":0.0}}'
        )
        try:
            response = await self._client.complete(prompt, temperature=0.2)
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if m:
                d = json.loads(m.group())
                return MarketScanResult(
                    d.get("regime_assessment", ""),
                    d.get("top_opportunities", []),
                    d.get("risks", []),
                    float(d.get("confidence", 0.5)),
                )
        except Exception as exc:
            _LOG.error("scan_failed err=%s", exc)
        return MarketScanResult("", [], [], 0.0)
