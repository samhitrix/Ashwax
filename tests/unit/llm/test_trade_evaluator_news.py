"""Unit tests for news wiring in src/llm/trade_evaluator.py (G9 advisory)."""

from __future__ import annotations

import pytest
from src.llm.trade_evaluator import _build_prompt, evaluate_trade


class _StubClient:
    def __init__(self, raw: str) -> None:
        self._raw = raw

    async def complete(self, prompt: str, system: str = "") -> str:
        return self._raw


def test_build_prompt_includes_recent_news() -> None:
    prompt = _build_prompt({"symbol": "X", "news_context": "BULLISH/HIGH (BOOST): foo — bar"})
    assert "recent_news:" in prompt
    assert "BULLISH/HIGH (BOOST): foo — bar" in prompt


def test_build_prompt_omits_recent_news_when_absent() -> None:
    assert "recent_news" not in _build_prompt({"symbol": "X"})


async def test_news_priority_aligned_applies_boost() -> None:
    raw = '{"confidence": 0.50, "flag": "CONFIRM", "reasoning": "ok"}'
    base = await evaluate_trade(_StubClient(raw), {"symbol": "X"})
    boosted = await evaluate_trade(_StubClient(raw), {"symbol": "X", "news_priority_aligned": True})
    assert boosted["llm_confidence"] == pytest.approx(base["llm_confidence"] + 0.10)


async def test_news_priority_misaligned_no_boost() -> None:
    raw = '{"confidence": 0.50, "flag": "CONFIRM", "reasoning": "ok"}'
    base = await evaluate_trade(_StubClient(raw), {"symbol": "X"})
    same = await evaluate_trade(_StubClient(raw), {"symbol": "X", "news_priority_aligned": False})
    assert same["llm_confidence"] == pytest.approx(base["llm_confidence"])
