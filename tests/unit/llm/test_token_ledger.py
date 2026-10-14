"""Per-purpose LLM token budgets: config-driven, refuse-when-spent, no borrowing."""

from __future__ import annotations

import pytest
from src.llm import token_ledger as tl
from src.llm.providers import LLMClient, LLMProvider
from src.llm.token_ledger import LLMBudgetExhausted, TokenLedger


@pytest.fixture()
def budgets(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    cfg: dict[str, object] = {"enabled": True, "daily_tokens": {"swing": 1000, "news_classify": 0}}
    monkeypatch.setattr(tl, "budget_config", lambda: cfg)
    return cfg


@pytest.mark.asyncio  # type: ignore[misc]
async def test_remaining_reflects_spend_and_config(budgets: dict[str, object]) -> None:
    led = TokenLedger()
    assert await led.remaining("swing") == 1000
    await led.add("swing", 300)
    assert await led.remaining("swing") == 700
    budgets["daily_tokens"] = {"swing": 5000}  # raised in config -> applies immediately
    assert await led.remaining("swing") == 4700


@pytest.mark.asyncio  # type: ignore[misc]
async def test_unlisted_or_disabled_is_unmetered(budgets: dict[str, object]) -> None:
    led = TokenLedger()
    assert await led.remaining("options") is None
    budgets["enabled"] = False
    assert await led.remaining("swing") is None


@pytest.mark.asyncio  # type: ignore[misc]
async def test_purposes_do_not_share_tokens(budgets: dict[str, object]) -> None:
    led = TokenLedger()
    await led.add("swing", 1000)
    assert await led.remaining("swing") == 0
    assert await led.remaining("news_classify") == 0  # its own (zero) share, unaffected
    budgets["daily_tokens"] = {"swing": 1000, "intraday": 500}
    assert await led.remaining("intraday") == 500


@pytest.mark.asyncio  # type: ignore[misc]
async def test_client_refuses_when_purpose_exhausted(
    budgets: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.llm.providers.LEDGER", TokenLedger())
    client = LLMClient(LLMProvider.GROQ, purpose="news_classify")  # share is 0
    with pytest.raises(LLMBudgetExhausted):
        await client.complete("hi")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_client_records_spend_and_skips_ollama(
    budgets: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = TokenLedger()
    monkeypatch.setattr("src.llm.providers.LEDGER", fresh)
    client = LLMClient(LLMProvider.GROQ, purpose="swing")

    async def _fake(self: LLMClient, prompt: str, *a: object, **k: object) -> str:
        self.last_provider = LLMProvider.GROQ
        self.last_tokens = 250
        return "{}"

    monkeypatch.setattr(LLMClient, "_complete_metered", _fake)
    await client.complete("x")
    assert await fresh.remaining("swing") == 750

    async def _ollama(self: LLMClient, prompt: str, *a: object, **k: object) -> str:
        self.last_provider = LLMProvider.OLLAMA
        self.last_tokens = 999
        return "{}"

    monkeypatch.setattr(LLMClient, "_complete_metered", _ollama)
    await client.complete("x")
    assert await fresh.remaining("swing") == 750  # local model spends nothing


def test_shipped_budgets_fit_provider_quota() -> None:
    cfg = tl.budget_config()
    assert cfg["enabled"] is True
    from src.llm.providers import _guard_cfg

    soft = set(_guard_cfg().get("soft_budget_purposes") or [])
    # Only HARD-capped purposes reserve share of the provider quota; soft (trade-path)
    # purposes alert but never block, so they are not counted against it.
    hard_total = sum(int(v) for k, v in cfg["daily_tokens"].items() if k not in soft)
    assert hard_total <= cfg["provider_daily_quota"]["groq"]
    assert soft <= set(cfg["daily_tokens"])
    assert {"news_classify", "swing", "intraday", "options"} <= set(cfg["daily_tokens"])


# ── Per-provider tracking (D1/D2, 2026-09-23) ──────────────────────────────────
# The per-PURPOSE ledger above cannot answer "why is Gemini exhausted" -- Gemini
# spend is split across swing/intraday/options plus an unmetered health-probe path.
# These guard the separate, unbudgeted provider dimension added to fix that.


@pytest.mark.asyncio  # type: ignore[misc]
async def test_provider_snapshot_starts_at_zero_and_accumulates() -> None:
    led = TokenLedger()
    snap = await led.provider_snapshot()
    assert snap["gemini"] == {"tokens": 0, "requests": 0}

    await led.add_provider_tokens("gemini", 150)
    await led.add_provider_request("gemini")
    await led.add_provider_request("gemini")
    snap = await led.provider_snapshot()
    assert snap["gemini"] == {"tokens": 150, "requests": 2}
    assert snap["groq"] == {"tokens": 0, "requests": 0}  # other providers unaffected


@pytest.mark.asyncio  # type: ignore[misc]
async def test_provider_tokens_can_be_corrected_down_without_going_negative() -> None:
    """The reconcile-on-return step in _dispatch corrects an over-estimate with a
    negative delta; the running total must floor at 0, not go negative."""
    led = TokenLedger()
    await led.add_provider_tokens("anthropic", 10)
    await led.add_provider_tokens("anthropic", -50)  # correction larger than the total
    assert await led.provider_tokens_spent("anthropic") == 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_purpose_snapshot_reflects_config_and_spend(budgets: dict[str, object]) -> None:
    led = TokenLedger()
    await led.add("swing", 300)
    snap = await led.purpose_snapshot()
    assert snap["swing"] == {"spent": 300, "limit": 1000}
    assert snap["news_classify"] == {"spent": 0, "limit": 0}


@pytest.mark.asyncio  # type: ignore[misc]
async def test_failed_dispatch_still_charges_the_provider_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-23 regression guard: today's 8 swing calls that never returned cost
    real Anthropic tokens and were recorded as zero, because LEDGER.add() only ran
    after a successful return. _dispatch now charges an estimate BEFORE the call, so
    a killed/failed attempt is never free."""
    fresh = TokenLedger()
    monkeypatch.setattr("src.llm.providers.LEDGER", fresh)
    client = LLMClient(LLMProvider.GROQ)

    async def _boom(*a: object, **k: object) -> str:
        raise TimeoutError("simulated hang")

    monkeypatch.setattr(client, "_dispatch_uncapped", _boom)
    with pytest.raises(TimeoutError):
        await client._dispatch(LLMProvider.GROQ, "a prompt here", "a system prompt", 0.3, 100)

    assert await fresh.provider_tokens_spent("groq") > 0
    assert await fresh.provider_requests_made("groq") == 1


@pytest.mark.asyncio  # type: ignore[misc]
async def test_successful_dispatch_reconciles_to_real_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """groq/cloudflare report real usage (self.last_tokens); the pre-dispatch estimate
    must be corrected to that real figure, not left as a guess."""
    fresh = TokenLedger()
    monkeypatch.setattr("src.llm.providers.LEDGER", fresh)
    client = LLMClient(LLMProvider.GROQ)

    async def _real_usage(*a: object, **k: object) -> str:
        client.last_tokens = 12345  # a value guaranteed to differ from the estimate
        return "{}"

    monkeypatch.setattr(client, "_dispatch_uncapped", _real_usage)
    await client._dispatch(LLMProvider.GROQ, "p", "s", 0.3, 100)

    assert await fresh.provider_tokens_spent("groq") == 12345


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dispatch_clears_last_tokens_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider that never reports usage (anthropic/gemini) must show None, not a
    stale value left over from an earlier successful hop to a DIFFERENT provider in
    the same fallback chain."""
    fresh = TokenLedger()
    monkeypatch.setattr("src.llm.providers.LEDGER", fresh)
    client = LLMClient(LLMProvider.ANTHROPIC)
    client.last_tokens = 999  # stale, from a hypothetical earlier successful hop

    async def _no_usage_reported(*a: object, **k: object) -> str:
        assert client.last_tokens is None, "last_tokens must be cleared before this attempt runs"
        return "{}"

    monkeypatch.setattr(client, "_dispatch_uncapped", _no_usage_reported)
    await client._dispatch(LLMProvider.ANTHROPIC, "p", "s", 0.3, 100)
    assert client.last_tokens is None
