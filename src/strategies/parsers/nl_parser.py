"""
NL parser â€” converts natural language strategy description to USSSpec via LLM.

The LLM client is injected so this module is testable without live API calls.
In production, inject a GeminiClient; in tests, inject a stub.
Sets source_format="nl" on the resulting spec.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from src.infrastructure.logger import get_logger

from ..uss.schema import USSSpec
from .json_parser import JSONParseError, JSONParser

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_NL_PROMPT = """\
You are a trading strategy parser for an Indian equity trading system.
Convert the following natural-language strategy description into a JSON strategy spec.

Return ONLY valid JSON with this exact structure:
{{
  "meta": {{
    "name": "<snake_case_name>",
    "display_name": "<Human Readable Name>",
    "product": "<DELIVERY|MIS|NRML>",
    "direction": "<LONG|SHORT>",
    "timeframe": "<TICK|1MIN|5MIN|15MIN|30MIN|1HOUR|DAILY|WEEKLY>"
  }},
  "universe_filter": {{
    "min_price": <number>,
    "min_adv_crores": <number>,
    "exchanges": ["NSE"]
  }},
  "entry_conditions": {{
    "logic": "<AND|OR>",
    "conditions": [
      {{
        "name": "<condition_name>",
        "type": "<PRICE|INDICATOR|VOLUME|PATTERN|COMPOSITE>"
      }}
    ]
  }},
  "stop_loss": {{
    "method": "<pattern_level|atr_based|fixed_pct|fixed_points|recent_low|recent_high>",
    "buffer_pct": <number>,
    "max_sl_pct": <number>
  }},
  "target": {{
    "method": "<risk_reward|fixed_pct|fixed_points|indicator>",
    "ratio": <number>
  }},
  "exit_conditions": {{}}
}}

Strategy description: {text}"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class NLParseError(ValueError):
    pass


class NLParser:
    """Parses natural language strategy description â†’ USSSpec via an LLM."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._json_parser = JSONParser()

    async def parse(self, text: str) -> USSSpec:
        if not text.strip():
            raise NLParseError("Strategy description cannot be empty")

        prompt = _NL_PROMPT.format(text=text)
        try:
            raw = await self._llm.complete(prompt)
        except Exception as exc:
            raise NLParseError(f"LLM call failed: {exc}") from exc

        data = _extract_json(raw)
        try:
            spec = self._json_parser.parse(data)
        except JSONParseError as exc:
            raise NLParseError(f"LLM output is not valid USS: {exc}") from exc

        return spec.model_copy(update={"source_format": "nl"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NLParseError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise NLParseError("LLM JSON root must be a mapping")
    return data
