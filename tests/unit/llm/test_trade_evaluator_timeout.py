"""G9 hard timeout — src/llm/trade_evaluator.py.

2026-08-25: config/global.json pointed ollama at an uninstalled model. Every intraday
signal spent 11-14s inside the G9 call before erroring to NEUTRAL anyway, and by the
time the entry reached the broker price had run past the marketable-limit chase cap.
The gate must fail fast: a dead provider costs a fixed, small delay, never the entry.
"""

from __future__ import annotations

import asyncio
import time

from src.llm.trade_evaluator import evaluate_trade


class _HangingClient:
    async def complete(self, prompt: str, system: str = "") -> str:
        await asyncio.sleep(30)
        return '{"confidence": 0.9, "flag": "CONFIRM", "reasoning": "never gets here"}'


class _FastClient:
    async def complete(self, prompt: str, system: str = "") -> str:
        return '{"confidence": 0.80, "flag": "CONFIRM", "reasoning": "ok"}'


async def test_slow_provider_is_cut_off_and_fails_open() -> None:
    t0 = time.perf_counter()
    result = await evaluate_trade(_HangingClient(), {"symbol": "AFFLE"}, timeout_secs=0.2)
    elapsed = time.perf_counter() - t0

    assert elapsed < 5.0, "G9 must not block the entry on a dead provider"
    assert result["evaluated"] is False  # fails OPEN — an LLM outage never halts trading
    assert result["llm_flag"] == "NEUTRAL"
    assert result["llm_confidence"] == 0.50
    assert "timeout" in result["llm_reason"].lower()


async def test_fast_provider_still_evaluated_normally() -> None:
    result = await evaluate_trade(_FastClient(), {"symbol": "AFFLE"}, timeout_secs=5.0)

    assert result["evaluated"] is True
    assert result["llm_flag"] == "CONFIRM"
