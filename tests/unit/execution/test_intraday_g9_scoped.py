"""G9 on the intraday path for LLM-ORIGINATED strategies (registry llm_originated / llm_gate_required).

llm_intraday_agent's ideas come from the gemini-first chain, and the standard
intraday G9 client (trade_gate) is gemini-first too, so an LLM-originated strategy is
reviewed by the separate intraday_g9 client instead and FAILS CLOSED when no
verdict comes back — the LLM is its only basis. Strategies with llm_gate_required=false keep the
exact pre-existing fail-open path. Mirrors the swing G9 tests in
tests/unit/execution/test_swing_signal.py.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from src.execution.order_lifecycle import OrderState
from src.orchestrator import _execute_intraday_signal


class _Lifecycle:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def place_entry_protection_and_poll(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            state=OrderState.FILLED,
            agent_trade_id="TRD-TEST-1",
            fill_price=100.0,
            filled_quantity=int(kwargs["qty"]),
            sl_price=99.0,
            target_price=102.5,
            sl_order_id="SL-1",
            target_order_id="TGT-1",
        )


def _sig() -> SimpleNamespace:
    return SimpleNamespace(
        entry_price=100.0, sl=99.0, target=102.5, direction=SimpleNamespace(value="LONG")
    )


def _shared(**kw: Any) -> tuple[dict[str, Any], _Lifecycle, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def _evaluate(sig: Any, ctx: Any) -> SimpleNamespace:
        captured["sig"] = sig
        return SimpleNamespace(
            approved=True, final_quantity=5, blocking_gate=None, blocking_reason=None
        )

    lc = _Lifecycle()
    shared: dict[str, Any] = {
        "paper_mode": True,
        "lifecycle": lc,
        "order_monitor": SimpleNamespace(get_monitored=lambda: [], track=lambda pos: None),
        "news_avoid_symbols": {},
        "g9_llm": None,
        "current_equity": 200000.0,
        "held_symbols_loaded": True,
        "llm_originated_strategies": {"llm_intraday_agent"},
        "crm": SimpleNamespace(evaluate=_evaluate),
        # llm_bypass=True must NOT let a gated strategy skip its review.
        "crm_ctx": SimpleNamespace(
            current_equity=200000.0,
            llm_bypass=True,
            max_risk_per_trade=1000.0,
            default_risk_pct=0.0,
            max_risk_per_trade_pct_ceiling=0.0,
            mis_leverage=5.0,
            llm_gate_required={"llm_intraday_agent": True, "news_catalyst_momentum": False},
        ),
    }
    shared.update(kw)
    return shared, lc, captured


def _patch_evaluate(
    monkeypatch: pytest.MonkeyPatch, evaluated: bool, flag: str = "CONFIRM"
) -> list[Any]:
    clients: list[Any] = []

    async def _fake(
        client: Any, sig: dict[str, Any], consecutive_malformed: int = 0, **_: Any
    ) -> dict[str, Any]:  # serves evaluate_trade and evaluate_trade_gated
        clients.append(client)
        return {
            "llm_confidence": 0.8,
            "llm_flag": flag,
            "llm_reason": "r",
            "malformed": 0,
            "latency_ms": 5,
            "evaluated": evaluated,
        }

    import src.llm.trade_evaluator as _te

    monkeypatch.setattr(_te, "evaluate_trade", _fake)
    monkeypatch.setattr(_te, "evaluate_trade_gated", _fake)
    return clients


async def _run(shared: dict[str, Any], strategy: str = "llm_intraday_agent") -> Any:
    return await _execute_intraday_signal(shared, strategy, "EICHERMOT", _sig(), datetime.now())


@pytest.mark.asyncio  # type: ignore[misc]
async def test_listed_strategy_uses_scoped_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    reviewer = MagicMock(name="intraday_g9_llm")
    shared, lc, captured = _shared(intraday_g9_llm=reviewer, g9_llm=MagicMock(name="gemini"))
    clients = _patch_evaluate(monkeypatch, evaluated=True)

    assert await _run(shared) is not None
    assert clients == [reviewer]  # never the gemini-first trade_gate client
    assert captured["sig"].meta["llm_flag"] == "CONFIRM"
    assert captured["sig"].meta["llm_evaluated"] is True
    assert len(lc.calls) == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_listed_strategy_fails_closed_without_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared, lc, captured = _shared(intraday_g9_llm=MagicMock())
    _patch_evaluate(monkeypatch, evaluated=False)

    assert await _run(shared) is None
    assert "sig" not in captured  # CRM never consulted
    assert lc.calls == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_listed_strategy_fails_closed_without_provider() -> None:
    shared, lc, captured = _shared(intraday_g9_llm=None)

    assert await _run(shared) is None
    assert "sig" not in captured
    assert lc.calls == []


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unlisted_strategy_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No G9 client and llm_bypass → the pre-existing path places with no LLM call."""
    shared, lc, _ = _shared(intraday_g9_llm=MagicMock())
    clients = _patch_evaluate(monkeypatch, evaluated=False)

    assert await _run(shared, strategy="news_catalyst_momentum") is not None
    assert clients == []
    assert len(lc.calls) == 1
