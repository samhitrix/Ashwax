"""Tests for NLParser."""

import json

import pytest
from src.strategies.parsers.nl_parser import NLParseError, NLParser

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

_VALID_USS_JSON = {
    "meta": {
        "name": "momentum_breakout",
        "product": "DELIVERY",
        "direction": "LONG",
        "timeframe": "DAILY",
    },
    "universe_filter": {"min_price": 50, "min_adv_crores": 5, "exchanges": ["NSE"]},
    "entry_conditions": {
        "logic": "AND",
        "conditions": [{"name": "price_above_sma200", "type": "INDICATOR"}],
    },
    "stop_loss": {"method": "recent_low", "buffer_pct": 0.5, "max_sl_pct": 8.0},
    "target": {"method": "risk_reward", "ratio": 2.0},
    "exit_conditions": {},
}


class _OkLLM:
    def __init__(self, payload: object) -> None:
        self._response = json.dumps(payload)

    async def complete(self, prompt: str) -> str:
        return self._response


class _MarkdownLLM:
    """Returns JSON wrapped in a markdown code block."""

    def __init__(self, payload: object) -> None:
        self._response = f"```json\n{json.dumps(payload)}\n```"

    async def complete(self, prompt: str) -> str:
        return self._response


class _FailLLM:
    async def complete(self, prompt: str) -> str:
        raise RuntimeError("API timeout")


class _BadJsonLLM:
    async def complete(self, prompt: str) -> str:
        return "this is not json"


class _BadStructureLLM:
    """Returns valid JSON but missing required USS fields."""

    async def complete(self, prompt: str) -> str:
        return json.dumps({"meta": {"name": "x"}})


# ---------------------------------------------------------------------------
# Normal cases
# ---------------------------------------------------------------------------


async def test_valid_llm_response_returns_spec() -> None:
    parser = NLParser(_OkLLM(_VALID_USS_JSON))
    spec = await parser.parse("Buy when price breaks above 200-day SMA with R:R of 2:1")
    assert spec.meta.name == "momentum_breakout"
    assert spec.source_format == "nl"


async def test_markdown_code_block_stripped() -> None:
    parser = NLParser(_MarkdownLLM(_VALID_USS_JSON))
    spec = await parser.parse("Buy on breakout with ATR stop")
    assert spec.meta.name == "momentum_breakout"
    assert spec.source_format == "nl"


async def test_target_ratio_preserved() -> None:
    parser = NLParser(_OkLLM(_VALID_USS_JSON))
    spec = await parser.parse("any text")
    assert spec.target.ratio == 2.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_empty_text_raises() -> None:
    parser = NLParser(_OkLLM(_VALID_USS_JSON))
    with pytest.raises(NLParseError, match="empty"):
        await parser.parse("   ")


async def test_whitespace_only_raises() -> None:
    parser = NLParser(_OkLLM(_VALID_USS_JSON))
    with pytest.raises(NLParseError):
        await parser.parse("\n\t")


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


async def test_llm_failure_raises() -> None:
    parser = NLParser(_FailLLM())
    with pytest.raises(NLParseError, match="LLM call failed"):
        await parser.parse("some strategy description")


async def test_bad_json_raises() -> None:
    parser = NLParser(_BadJsonLLM())
    with pytest.raises(NLParseError, match="invalid JSON"):
        await parser.parse("some strategy description")


async def test_bad_uss_structure_raises() -> None:
    parser = NLParser(_BadStructureLLM())
    with pytest.raises(NLParseError, match="not valid USS"):
        await parser.parse("some strategy description")
