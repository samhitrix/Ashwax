"""Registry llm_gate_required / llm_originated policy + the gated evaluator's failover."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from src.llm.gate_policy import GatePolicyError, load_gate_policy, parse_gate_policy
from src.llm.trade_evaluator import evaluate_trade_gated


def test_missing_key_defaults_to_required() -> None:
    required, originated = parse_gate_policy([{"name": "new_strategy"}])
    assert required == {"new_strategy": True}
    assert originated == set()


def test_explicit_false_is_respected() -> None:
    required, _ = parse_gate_policy([{"name": "dsl", "llm_gate_required": False}])
    assert required == {"dsl": False}


def test_llm_originated_always_required() -> None:
    required, originated = parse_gate_policy([{"name": "llm_swing_agent", "llm_originated": True}])
    assert required == {"llm_swing_agent": True}
    assert originated == {"llm_swing_agent"}


def test_llm_originated_cannot_disable_gate() -> None:
    with pytest.raises(GatePolicyError):
        parse_gate_policy(
            [{"name": "llm_swing_agent", "llm_originated": True, "llm_gate_required": False}]
        )


def test_shipped_registry_gates_both_llm_strategies() -> None:
    required, originated = load_gate_policy()
    assert {"llm_swing_agent", "llm_intraday_agent"} <= originated
    assert required["llm_swing_agent"] and required["llm_intraday_agent"]
    raw = json.loads(Path("config/strategies_registry.json").read_text(encoding="utf-8"))
    assert all(
        "llm_gate_required" in e or e.get("llm_originated") for e in raw["strategies"]
    ), "every registry entry must state its LLM gate policy explicitly"


class _SlowThenGood:
    """First provider hangs longer than a 3s cap; the chain must still reach the second."""

    def __init__(self, first_delay: float) -> None:
        self.first_delay = first_delay
        self.calls = 0
        self.last_provider: Any = None

    async def complete(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(self.first_delay)
        self.last_provider = SimpleNamespace(value="cloudflare")
        return '{"confidence": 0.8, "flag": "CONFIRM", "reasoning": "ok"}'


@pytest.mark.asyncio  # type: ignore[misc]
async def test_gated_evaluator_uses_chain_budget_not_3s_cap() -> None:
    client = _SlowThenGood(first_delay=0.2)
    v = await evaluate_trade_gated(client, {"symbol": "X"}, 0, chain_timeout_secs=5.0)
    assert v["evaluated"] is True and v["provider"] == "cloudflare"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_gated_evaluator_rechecks_once_before_no_verdict() -> None:
    client = _SlowThenGood(first_delay=1.0)  # first pass times out at 0.3s
    v = await evaluate_trade_gated(client, {"symbol": "X"}, 0, chain_timeout_secs=0.3)
    assert v["attempts"] == 2
    assert v["evaluated"] is True  # second pass succeeded


@pytest.mark.asyncio  # type: ignore[misc]
async def test_gated_evaluator_reports_attempts_when_all_fail() -> None:
    class _Dead:
        async def complete(self, prompt: str, system: str = "") -> str:
            raise RuntimeError("down")

    v = await evaluate_trade_gated(_Dead(), {"symbol": "X"}, 0, chain_timeout_secs=1.0)
    assert v["evaluated"] is False
    assert v["attempts"] == 2
    assert "no verdict after 2 attempts" in v["llm_reason"]
