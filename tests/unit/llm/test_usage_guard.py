"""LLM usage guardrails (2026-09-24): trade-path calls are never blocked by a counter, but
every failure / waste condition is loud (dashboard Alerts tab + Telegram)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from src.infrastructure import operator_alerts as oa
from src.infrastructure.alert_system import AlertManager
from src.llm import providers as prov
from src.llm.providers import LLMClient, LLMProvider
from src.llm.token_ledger import LEDGER, LLMBudgetExhausted

GUARD = {
    "alert_purposes": ["swing", "intraday"],
    "soft_budget_purposes": ["swing", "intraday"],
    "max_prompt_chars": {"swing": 1000, "default": 0},
    "duplicate_window_seconds": 900,
    "failover_alert_dedupe_minutes": 10,
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    oa._reset_for_testing()
    mgr = AlertManager()
    oa.configure(mgr)
    LEDGER._mem.clear()
    LEDGER._mem_seen.clear()
    LEDGER._redis = None
    monkeypatch.setattr(prov, "_guard_cfg", lambda: dict(GUARD))
    monkeypatch.setattr(prov._RATE_LIMITER, "_min_interval", 0.0)
    with patch("src.notifications.notify") as n:
        yield type("E", (), {"mgr": mgr, "notify": n})
    oa._reset_for_testing()
    LEDGER._mem.clear()
    LEDGER._mem_seen.clear()


def _exhaust(purpose: str, limit: int = 100) -> None:
    key = LEDGER._key(purpose)
    LEDGER._mem[key] = limit + 5


def _budget(monkeypatch, limits: dict[str, int]) -> None:
    monkeypatch.setattr("src.llm.providers.daily_limit", lambda p: limits.get(p))
    monkeypatch.setattr("src.llm.token_ledger.daily_limit", lambda p: limits.get(p))


@pytest.mark.asyncio
async def test_soft_purpose_over_budget_is_NOT_blocked_but_alerts(_env, monkeypatch) -> None:
    _budget(monkeypatch, {"swing": 100})
    _exhaust("swing")
    c = LLMClient(LLMProvider.GROQ, purpose="swing")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value='{"ok":1}')) as m:
        out = await c.complete("p", system="s")
    assert out == '{"ok":1}'
    m.assert_awaited_once()  # the main call went out
    assert any("budget" in a.message for a in _env.mgr.active())


@pytest.mark.asyncio
async def test_hard_purpose_over_budget_is_refused_with_a_clear_reason(monkeypatch) -> None:
    _budget(monkeypatch, {"news_classify": 100})
    _exhaust("news_classify")
    c = LLMClient(LLMProvider.GROQ, purpose="news_classify")
    with (
        patch.object(LLMClient, "_complete_metered", new=AsyncMock()) as m,
        pytest.raises(LLMBudgetExhausted) as ei,
    ):
        await c.complete("p")
    m.assert_not_called()
    msg = str(ei.value)
    assert "spent=105" in msg and "limit=100" in msg and "BEFORE any provider was tried" in msg


@pytest.mark.asyncio
async def test_oversized_prompt_alerts_but_still_sends(_env, monkeypatch) -> None:
    _budget(monkeypatch, {})
    c = LLMClient(LLMProvider.GROQ, purpose="swing")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")) as m:
        await c.complete("x" * 5000, system="s")
    m.assert_awaited_once()
    assert any("oversized" in a.message for a in _env.mgr.active())


@pytest.mark.asyncio
async def test_identical_prompt_twice_raises_a_duplicate_alert_but_is_not_suppressed(
    _env, monkeypatch
) -> None:
    _budget(monkeypatch, {})
    # grace 0 + no reuse: no repeat counts as the caller's own retry, so it is a true duplicate
    monkeypatch.setattr(
        prov,
        "_guard_cfg",
        lambda: {**GUARD, "duplicate_retry_grace_seconds": 0, "reuse_duplicates": False},
    )
    c = LLMClient(LLMProvider.GROQ, purpose="intraday")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")) as m:
        await c.complete("same prompt", system="s")
        assert not any("DUPLICATE" in a.message for a in _env.mgr.active())
        await c.complete("same prompt", system="s")
    assert m.await_count == 2  # a legitimate retry still goes through
    assert any("DUPLICATE" in a.message for a in _env.mgr.active())


@pytest.mark.asyncio
async def test_repeat_inside_retry_grace_is_a_retry_not_a_duplicate_alert(
    _env, monkeypatch
) -> None:
    # 2026-10-13: G9 timed out at 60s and evaluate_trade_gated re-sent the identical prompt
    # once; that deliberate retry raised "DUPLICATE LLM call" although nothing was looping.
    _budget(monkeypatch, {})
    c = LLMClient(LLMProvider.GROQ, purpose="intraday")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")) as m:
        await c.complete("same prompt", system="s")
        await c.complete("same prompt", system="s")
    assert m.await_count == 2  # the retry still reaches the LLM
    assert not any("DUPLICATE" in a.message for a in _env.mgr.active())


@pytest.mark.asyncio
async def test_only_the_first_repeat_in_grace_is_excused(_env, monkeypatch) -> None:
    # A 3rd identical send inside the grace (restart/retry loop) must still alert.
    _budget(monkeypatch, {})
    c = LLMClient(LLMProvider.GROQ, purpose="intraday")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")):
        await c.complete("loop prompt", system="s")
        await c.complete("loop prompt", system="s")
        assert not any("DUPLICATE" in a.message for a in _env.mgr.active())
        await c.complete("loop prompt", system="s")
    assert any("DUPLICATE" in a.message for a in _env.mgr.active())


@pytest.mark.asyncio
async def test_non_alert_purposes_stay_quiet(_env, monkeypatch) -> None:
    _budget(monkeypatch, {})
    c = LLMClient(LLMProvider.GROQ, purpose="news_classify")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")):
        await c.complete("x" * 5000)
        await c.complete("x" * 5000)
    assert _env.mgr.active() == []


@pytest.mark.asyncio
async def test_provider_failure_and_failover_raise_a_critical_alert(_env, monkeypatch) -> None:
    _budget(monkeypatch, {})
    monkeypatch.setattr("src.llm.providers._provider_available", lambda p: True)
    c = LLMClient(LLMProvider.ANTHROPIC, fallbacks=[LLMProvider.GROQ], purpose="swing")
    calls: list[LLMProvider] = []

    async def fake(self, provider, *a, **k):
        calls.append(provider)
        if provider == LLMProvider.ANTHROPIC:
            raise RuntimeError("anthropic overloaded 529")
        return '{"answer": 1}'

    with patch.object(LLMClient, "_dispatch_with_retry", new=fake):
        out = await c.complete("a fresh prompt", system="s")
    assert out == '{"answer": 1}' and calls == [LLMProvider.ANTHROPIC, LLMProvider.GROQ]
    msgs = [a.message for a in _env.mgr.active()]
    assert any("'anthropic' FAILED" in m and "Failing over" in m for m in msgs)
    assert _env.notify.called  # Telegram too


@pytest.mark.asyncio
async def test_total_outage_raises_all_providers_failed(_env, monkeypatch) -> None:
    _budget(monkeypatch, {})
    monkeypatch.setattr("src.llm.providers._provider_available", lambda p: True)
    c = LLMClient(LLMProvider.ANTHROPIC, fallbacks=[LLMProvider.GROQ], purpose="swing")

    async def boom(self, provider, *a, **k):
        raise RuntimeError(f"{provider.value} down")

    with patch.object(LLMClient, "_dispatch_with_retry", new=boom), pytest.raises(RuntimeError):
        await c.complete("another prompt", system="s")
    assert any("ALL LLM providers failed" in a.message for a in _env.mgr.active())


async def test_identical_prompt_after_retry_grace_is_served_from_cache(_env, monkeypatch) -> None:
    # 2026-10-06: the options desk re-sent the same review every 5 min. After the retry
    # grace an identical prompt must reuse the earlier answer -- no second LLM call.
    _budget(monkeypatch, {})
    LEDGER._mem_resp.clear()
    c = LLMClient(LLMProvider.GROQ, purpose="options")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value='{"v":1}')) as m:
        assert await c.complete("same", "sys") == '{"v":1}'
        key = c._dup_key("same", "sys")
        ts, payload = LEDGER._mem_resp[key]
        LEDGER._mem_resp[key] = (ts - 300, payload)  # 5 minutes later
        assert await c.complete("same", "sys") == '{"v":1}'
        assert m.await_count == 1


async def test_identical_prompt_within_retry_grace_still_calls(_env, monkeypatch) -> None:
    # A caller's immediate retry of a malformed reply must reach the LLM again.
    _budget(monkeypatch, {})
    LEDGER._mem_resp.clear()
    c = LLMClient(LLMProvider.GROQ, purpose="options")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")) as m:
        await c.complete("same", "sys")
        await c.complete("same", "sys")
        assert m.await_count == 2


async def test_different_prompt_is_never_served_from_cache(_env, monkeypatch) -> None:
    _budget(monkeypatch, {})
    LEDGER._mem_resp.clear()
    c = LLMClient(LLMProvider.GROQ, purpose="options")
    with patch.object(LLMClient, "_complete_metered", new=AsyncMock(return_value="{}")) as m:
        await c.complete("a", "sys")
        await c.complete("b", "sys")
        assert m.await_count == 2
