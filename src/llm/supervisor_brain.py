"""Supervisor AI Brain — periodic LLM analysis of system state."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from src.llm.providers import LLMClient

_LOG = logging.getLogger("llm.supervisor_brain")


@dataclass
class BrainAnalysis:
    risk_assessment: str
    drift_detected: bool
    game_plan: str
    confidence: float


class SupervisorBrain:
    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    async def analyse(self, state: dict[str, Any]) -> BrainAnalysis:
        prompt = f"Analyse trading system state:\n{state}\nJSON: {{risk_assessment,drift_detected,game_plan,confidence}}"
        try:
            response = await self._client.complete(prompt, temperature=0.1)
            m = re.search(r"\{.*\}", response, re.DOTALL)
            if m:
                d = json.loads(m.group())
                return BrainAnalysis(
                    d.get("risk_assessment", ""),
                    bool(d.get("drift_detected", False)),
                    d.get("game_plan", ""),
                    float(d.get("confidence", 0.5)),
                )
        except Exception as exc:
            _LOG.error("brain_failed err=%s", exc)
        return BrainAnalysis("", False, "", 0.0)
