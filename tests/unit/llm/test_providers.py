"""Unit tests for LLMClient bidirectional provider fallback (src/llm/providers.py)."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from src.llm import providers as _prov
from src.llm.providers import LLMClient, LLMProvider, make_trade_llm_client


@pytest.fixture(autouse=True)
def _no_global_throttle():
    """Neutralize the process-global rate limiter so fallback tests stay fast."""
    from src.llm import providers

    orig = providers._RATE_LIMITER._min_interval
    providers._RATE_LIMITER._min_interval = 0.0
    providers._RATE_LIMITER._last = 0.0
    yield
    providers._RATE_LIMITER._min_interval = orig


async def test_rate_limiter_enforces_min_interval() -> None:
    from src.llm.providers import _RateLimiter

    rl = _RateLimiter(max_per_minute=60)  # 1s spacing
    slept: list[float] = []
    with (
        patch("src.llm.providers.time.monotonic", return_value=100.0),
        patch(
            "src.llm.providers.asyncio.sleep", new=AsyncMock(side_effect=lambda s: slept.append(s))
        ),
    ):
        await rl.acquire()  # first call — no wait
        await rl.acquire()  # immediate second call — must wait ~1s
    assert slept and slept[-1] == pytest.approx(1.0)


async def test_gemini_exhausted_falls_back_to_groq() -> None:
    c = LLMClient(LLMProvider.GEMINI)
    with (
        patch.dict(os.environ, {"GROQ_API_KEY": "x", "GEMINI_API_KEY": "y"}),
        patch.object(c, "_gemini", new=AsyncMock(side_effect=RuntimeError("quota exhausted"))),
        patch.object(c, "_groq", new=AsyncMock(return_value="GROQ_OK")),
    ):
        assert await c.complete("p") == "GROQ_OK"


async def test_groq_ratelimited_falls_back_to_gemini() -> None:
    c = LLMClient(LLMProvider.GROQ)
    with (
        patch.dict(os.environ, {"GROQ_API_KEY": "x", "GEMINI_API_KEY": "y"}),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("429"))),
        patch.object(c, "_gemini", new=AsyncMock(return_value="GEMINI_OK")),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()),  # skip real backoff
    ):
        assert await c.complete("p") == "GEMINI_OK"


async def test_no_other_provider_reraises() -> None:
    c = LLMClient(LLMProvider.GROQ)
    with (
        patch.dict(os.environ, {"GROQ_API_KEY": "x"}, clear=True),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await c.complete("p")


# ── Explicit trade-gate chain (GEMINI → GROQ → OLLAMA) ────────────────────────


async def test_trade_chain_falls_through_to_ollama() -> None:
    """When the cloud providers both error, the chain reaches local Ollama."""
    c = LLMClient(LLMProvider.GEMINI, fallbacks=[LLMProvider.GROQ, LLMProvider.OLLAMA])
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y", "GROQ_API_KEY": "x"}),
        patch.object(c, "_gemini", new=AsyncMock(side_effect=RuntimeError("quota"))),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("429"))),
        patch.object(c, "_ollama", new=AsyncMock(return_value="OLLAMA_OK")),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()),  # skip real backoff
    ):
        assert await c.complete("p") == "OLLAMA_OK"


async def test_trade_chain_skips_provider_without_key() -> None:
    """A cloud provider with no API key is skipped, not attempted."""
    c = LLMClient(LLMProvider.GEMINI, fallbacks=[LLMProvider.GROQ, LLMProvider.OLLAMA])
    groq = AsyncMock(return_value="GROQ_OK")
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True),  # no GROQ_API_KEY
        patch.object(c, "_gemini", new=AsyncMock(side_effect=RuntimeError("quota"))),
        patch.object(c, "_groq", new=groq),
        patch.object(c, "_ollama", new=AsyncMock(return_value="OLLAMA_OK")),
    ):
        assert await c.complete("p") == "OLLAMA_OK"
    groq.assert_not_awaited()


def test_make_trade_llm_client_chain_comes_from_config_and_skips_missing_keys() -> None:
    """trade_gate order is config-driven (llm.trade_gate) and filtered to providers that
    actually have credentials — ollama stays out of the gate chains entirely."""
    with patch.dict(os.environ, {"GEMINI_API_KEY": "y", "GROQ_API_KEY": "x"}, clear=True):
        client = make_trade_llm_client()
    assert client is not None
    chain = [client._provider, *(client._fallbacks or [])]
    assert LLMProvider.OLLAMA not in chain
    assert LLMProvider.ANTHROPIC not in chain  # no ANTHROPIC_API_KEY in this env
    assert set(chain) <= {LLMProvider.GEMINI, LLMProvider.GROQ, LLMProvider.CLOUDFLARE}


def test_make_trade_llm_client_none_when_no_keys() -> None:
    """No cloud key -> no client -> the gate fails closed (a 7B local model is not a fallback)."""
    with patch.dict(os.environ, {}, clear=True):
        assert make_trade_llm_client() is None


# ── 429 backoff / retry ──────────────────────────────────────────────────────


def test_is_rate_limited_detects_quota_errors() -> None:
    from src.llm.providers import _is_rate_limited

    assert _is_rate_limited(RuntimeError("429 Too Many Requests"))
    assert _is_rate_limited(RuntimeError("RESOURCE_EXHAUSTED"))
    assert _is_rate_limited(RuntimeError("rate limit reached"))
    assert not _is_rate_limited(RuntimeError("invalid api key"))


def test_is_payment_required_matches_gemini_prepaid_402_not_429_quota() -> None:
    from src.llm.providers import _is_payment_required

    gemini_402 = (
        "ClientError: 402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': "
        "'Your prepayment credits are depleted.'}}"
    )
    gemini_429 = "ClientError: 429 RESOURCE_EXHAUSTED. You exceeded your current quota"
    assert _is_payment_required(RuntimeError(gemini_402))
    assert _is_payment_required(RuntimeError("status=402 payment_required"))
    assert not _is_payment_required(RuntimeError(gemini_429))


async def test_429_retries_same_provider_then_succeeds() -> None:
    c = LLMClient(LLMProvider.GROQ)
    calls = {"n": 0}

    def _flaky(*_a: object, **_k: object) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return "OK_AFTER_RETRY"

    with (
        patch.dict(os.environ, {"GROQ_API_KEY": "x"}, clear=True),
        patch.object(c, "_groq", new=AsyncMock(side_effect=_flaky)),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        assert await c.complete("p") == "OK_AFTER_RETRY"
    assert calls["n"] == 2  # retried exactly once before success
    assert sleep_mock.await_count == 1  # one backoff


async def test_429_exhausts_retries_then_reraises() -> None:
    c = LLMClient(LLMProvider.GROQ)
    with (
        patch.dict(os.environ, {"GROQ_API_KEY": "x"}, clear=True),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("429"))),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        pytest.raises(RuntimeError, match="429"),
    ):
        await c.complete("p")
    assert sleep_mock.await_count == 2  # max_retries backoffs before giving up


# ── Truncated-response retry (2026-09-16: a 'thinking' model's internal ──────
# reasoning tokens ate the visible-output budget before the JSON closed, and
# the old success check accepted the partial text — llm_swing_agent
# and llm_intraday_agent silently lost real trade days to it, undetected)


def test_is_truncated_detects_the_tagged_error() -> None:
    from src.llm.providers import _is_truncated

    assert _is_truncated(RuntimeError("gemini_truncated_response finish_reason=MAX_TOKENS"))
    assert not _is_truncated(RuntimeError("429 Too Many Requests"))
    assert not _is_truncated(RuntimeError("groq_no_choices: quota exhausted"))


async def test_truncated_response_retries_with_escalated_token_budget() -> None:
    """No sleep (unlike a 429 backoff — there's no external throttle to respect)
    and each retry raises the token budget instead of repeating the same one,
    since a same-budget retry tends to hit the identical wall again."""
    c = LLMClient(LLMProvider.GEMINI)
    seen_tokens: list[int] = []

    async def _flaky(prompt: str, system: str, temperature: float, max_tokens: int) -> str:
        seen_tokens.append(max_tokens)
        if len(seen_tokens) < 2:
            raise RuntimeError("gemini_truncated_response finish_reason=MAX_TOKENS len=260")
        return "OK_AFTER_ESCALATION"

    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True),
        patch.object(c, "_gemini", new=_flaky),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        result = await c.complete("p", max_tokens=1500)
    assert result == "OK_AFTER_ESCALATION"
    assert seen_tokens == [1500, 2700]  # escalated 1.8x, not repeated
    assert sleep_mock.await_count == 0  # truncation doesn't back off


async def test_truncated_response_exhausts_retries_then_reraises() -> None:
    c = LLMClient(LLMProvider.GEMINI)
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True),
        patch.object(
            c,
            "_gemini",
            new=AsyncMock(
                side_effect=RuntimeError("gemini_truncated_response finish_reason=MAX_TOKENS")
            ),
        ),
        pytest.raises(RuntimeError, match="gemini_truncated_response"),
    ):
        await c.complete("p")


async def test_gemini_raises_on_truncated_but_json_shaped_text() -> None:
    """The actual bug: real, non-empty, '{'-containing text that stopped for a
    reason other than STOP must be treated as a failure, not a success — the
    old code only checked for empty/non-JSON text."""

    class _Candidate:
        class _FR:
            name = "MAX_TOKENS"

        finish_reason = _FR()

    class _Resp:
        text = '```json\n{"thesis": "cut off mid-sen'
        candidates = [_Candidate()]

    c = LLMClient(LLMProvider.GEMINI)
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True),
        patch("google.genai.Client") as mock_cls,
        pytest.raises(RuntimeError, match="gemini_truncated_response"),
    ):
        mock_cls.return_value.models.generate_content.return_value = _Resp()
        await c._gemini("p", "sys", 0.3, 1500)


async def test_gemini_accepts_clean_stop_completion() -> None:
    class _Candidate:
        class _FR:
            name = "STOP"

        finish_reason = _FR()

    class _Resp:
        text = '{"thesis": "complete", "ideas": []}'
        candidates = [_Candidate()]

    c = LLMClient(LLMProvider.GEMINI)
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True),
        patch("google.genai.Client") as mock_cls,
    ):
        mock_cls.return_value.models.generate_content.return_value = _Resp()
        out = await c._gemini("p", "sys", 0.3, 1500)
    assert out == _Resp.text


# ── Background client reserves Gemini for the live G9 gate ────────────────────


def test_make_background_llm_client_never_uses_gemini() -> None:
    from src.llm.providers import make_background_llm_client

    # Only a Gemini key → background still avoids Gemini; nothing else is available -> None.
    with patch.dict(os.environ, {"GEMINI_API_KEY": "y"}, clear=True):
        assert make_background_llm_client() is None


def test_make_background_llm_client_prefers_groq() -> None:
    from src.llm.providers import make_background_llm_client

    with patch.dict(os.environ, {"GROQ_API_KEY": "x", "GEMINI_API_KEY": "y"}, clear=True):
        client = make_background_llm_client()
    assert client is not None
    assert client._provider == LLMProvider.GROQ
    assert client._fallbacks == []  # never Gemini


def test_make_background_llm_client_never_uses_a_paid_provider() -> None:
    """2026-09-23: 'news should not be using any paid models' -- enforced in code, not
    trusted from config, because a future config edit inserting anthropic (or any paid
    provider) into llm.background must not silently start billing news classification.
    Config ordering alone is not a guarantee; it drifts."""
    from src.llm.providers import make_background_llm_client

    with (
        patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "a", "GROQ_API_KEY": "x", "GEMINI_API_KEY": "y"},
            clear=True,
        ),
        # Simulates the exact drift this guards against: anthropic sneaks into config.
        patch(
            "src.llm.providers._llm_cfg",
            return_value={"background": ["anthropic", "groq"]},
        ),
    ):
        client = make_background_llm_client()
    assert client is not None
    assert client._provider != LLMProvider.ANTHROPIC
    assert LLMProvider.ANTHROPIC not in (client._fallbacks or [])
    assert client._provider == LLMProvider.GROQ


# ── Anthropic provider (opt-in, never a default) ───────────────────────────────


def test_anthropic_not_available_without_key() -> None:
    from src.llm.providers import _provider_available

    with patch.dict(os.environ, {}, clear=True):
        assert not _provider_available(LLMProvider.ANTHROPIC)
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "z"}, clear=True):
        assert _provider_available(LLMProvider.ANTHROPIC)


def test_trade_gate_anthropic_is_pinned_to_a_fast_model() -> None:
    """Anthropic is still opt-in (it appears only because llm.trade_gate lists it), but
    trade_gate wraps the WHOLE chain in trade_gate_timeout_seconds (3s). Anthropic's global
    list leads with claude-sonnet-5 (measured 6074ms), which cannot answer inside that cap —
    so this chain MUST pin it to a fast model via llm.chain_models, or adding anthropic here
    silently converts the gate into a guaranteed timeout (and it is fail-closed for
    llm_gate_required strategies, i.e. no verdict = no trade)."""
    from src.llm.providers import anthropic_models, chain_models

    with patch.dict(
        os.environ,
        {"GEMINI_API_KEY": "y", "ANTHROPIC_API_KEY": "z", "GROQ_API_KEY": "x"},
        clear=True,
    ):
        client = make_trade_llm_client()
    assert client is not None
    chain = [client._provider, *(client._fallbacks or [])]
    if LLMProvider.ANTHROPIC in chain:
        pinned = client._models_for(LLMProvider.ANTHROPIC, anthropic_models())
        assert (
            pinned != anthropic_models()
        ), "trade_gate must override anthropic's slow default list"
        assert len(pinned) <= 2, "a 3s budget cannot walk a long model list"
        assert chain_models("trade_gate").get("anthropic"), "override must come from config"


def test_chain_models_override_is_per_chain() -> None:
    """The same provider must be able to bring different models to different chains —
    frontier model to origination (90s budget), fastest model to a tight gate."""
    from src.llm.providers import anthropic_models

    env = {"ANTHROPIC_API_KEY": "z", "GROQ_API_KEY": "x", "GEMINI_API_KEY": "y"}
    with patch.dict(os.environ, env, clear=True):
        gate = make_trade_llm_client()
        orig = _prov.make_discretionary_llm_client()
    assert gate is not None and orig is not None
    # origination keeps the full provider list (frontier model first); the gate does not
    assert orig._models_for(LLMProvider.ANTHROPIC, anthropic_models()) == anthropic_models()


def test_chain_models_absent_falls_back_to_provider_list() -> None:
    """A chain with no override entry uses the provider's normal list unchanged."""
    c = LLMClient(LLMProvider.ANTHROPIC, model_overrides=None)
    from src.llm.providers import anthropic_models

    assert c._models_for(LLMProvider.ANTHROPIC, anthropic_models()) == anthropic_models()


def test_chain_models_accepts_bare_string() -> None:
    """chain_models tolerates a single model given as a string, not just a list."""
    with patch.object(
        _prov,
        "_llm_cfg",
        return_value={"chain_models": {"x_gate": {"anthropic": "claude-haiku-4-5"}}},
    ):
        assert _prov.chain_models("x_gate") == {"anthropic": ["claude-haiku-4-5"]}
        assert _prov.chain_models("missing_chain") == {}


async def test_anthropic_chain_used_when_configured() -> None:
    c = LLMClient(LLMProvider.GEMINI, fallbacks=[LLMProvider.ANTHROPIC, LLMProvider.OLLAMA])
    with (
        patch.dict(os.environ, {"GEMINI_API_KEY": "y", "ANTHROPIC_API_KEY": "z"}),
        patch.object(c, "_gemini", new=AsyncMock(side_effect=RuntimeError("quota"))),
        patch.object(c, "_anthropic", new=AsyncMock(return_value="ANTHROPIC_OK")),
    ):
        assert await c.complete("p") == "ANTHROPIC_OK"


async def test_anthropic_non_json_response_raises() -> None:
    c = LLMClient(LLMProvider.ANTHROPIC)

    class _Block:
        type = "text"
        text = "I cannot answer that"

    class _Resp:
        stop_reason = "end_turn"
        content = [_Block()]

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "z"}, clear=True),
        # patches anthropic.Anthropic (the SYNC client, 2026-09-23) -- the call now
        # runs via asyncio.to_thread so a blocking SDK call can never starve the
        # event loop again (see _anthropic_one_sync docstring).
        patch("anthropic.Anthropic") as mock_cls,
    ):
        mock_client = mock_cls.return_value.with_options.return_value
        mock_client.messages.create = MagicMock(return_value=_Resp())
        with pytest.raises(RuntimeError, match="anthropic_non_json_response"):
            await c._anthropic("p", "sys", 0.3, 100)


async def test_anthropic_refusal_raises() -> None:
    c = LLMClient(LLMProvider.ANTHROPIC)

    class _Resp:
        stop_reason = "refusal"
        content = []

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "z"}, clear=True),
        patch("anthropic.Anthropic") as mock_cls,
    ):
        mock_client = mock_cls.return_value.with_options.return_value
        mock_client.messages.create = MagicMock(return_value=_Resp())
        with pytest.raises(RuntimeError, match="anthropic_refusal"):
            await c._anthropic("p", "sys", 0.3, 100)


async def test_anthropic_call_does_not_block_the_event_loop() -> None:
    """2026-09-23 regression guard: the swing origination pass's anthropic call
    stalled the supervisor heartbeat dead for 140+ seconds -- past the call's own
    60-75s timeout, meaning wait_for's own timer could not fire because nothing was
    being scheduled on the loop at all. The launcher's watchdog then killed the whole
    process, wiping every in-memory throttle and causing a restart loop. Proof this
    cannot happen again: while a slow synchronous anthropic call is in flight (run via
    asyncio.to_thread), a concurrent asyncio.sleep()-based heartbeat task must still
    tick on schedule -- if the call blocked the loop, the heartbeat would starve
    exactly the way it did that day."""
    import time as _time

    c = LLMClient(LLMProvider.ANTHROPIC)

    class _Resp:
        stop_reason = "end_turn"

        class _Block:
            type = "text"
            text = '{"ok": true}'

        content = [_Block()]

    def _slow_sync_call(**_kw: object) -> _Resp:
        _time.sleep(0.3)  # simulates a slow provider response, on the worker thread
        return _Resp()

    heartbeats = 0

    async def _heartbeat_loop() -> None:
        nonlocal heartbeats
        for _ in range(6):
            await asyncio.sleep(0.05)
            heartbeats += 1

    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "z"}, clear=True),
        patch("anthropic.Anthropic") as mock_cls,
    ):
        mock_client = mock_cls.return_value.with_options.return_value
        mock_client.messages.create = MagicMock(side_effect=_slow_sync_call)
        await asyncio.gather(c._anthropic("p", "sys", 0.3, 100), _heartbeat_loop())

    # The 0.3s sync call and the 6x0.05s=0.3s heartbeat loop overlap almost entirely;
    # if the anthropic call blocked the loop, heartbeats would be starved until the
    # call returned and this would be far below 6.
    assert heartbeats == 6, "heartbeat starved -- the anthropic call blocked the event loop"


# ── Discretionary-strategy client (once-daily reasoning, own provider chain) ──


def test_make_discretionary_llm_client_prefers_anthropic() -> None:
    """ORIGINATION runs anthropic-first (config llm.discretionary). Whatever originates
    here must stay OUT of the swing_g9/intraday_g9 review chains — a model reviewing its
    own idea is not an independent check."""
    from src.llm.providers import make_discretionary_llm_client

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "y", "GROQ_API_KEY": "x"},
        clear=True,
    ):
        client = make_discretionary_llm_client()
    assert client is not None
    assert client._provider == LLMProvider.ANTHROPIC
    assert LLMProvider.GROQ in client._fallbacks


def test_make_discretionary_llm_client_falls_back_without_anthropic_key() -> None:
    """No ANTHROPIC_API_KEY: anthropic is filtered out by _provider_available and the
    chain degrades to the next configured provider rather than returning None."""
    from src.llm.providers import make_discretionary_llm_client

    with patch.dict(os.environ, {"GROQ_API_KEY": "x"}, clear=True):
        client = make_discretionary_llm_client()
    assert client is not None
    assert client._provider == LLMProvider.GROQ


def test_origination_client_gets_transient_retries() -> None:
    """Regression guard: make_discretionary_llm_client used to be constructed WITHOUT
    transient_retries, so it defaulted to 0 — the highest-value call in the system gave
    up on its provider after a single flaky 5xx/non-JSON reply while every gate client
    retried. It must carry a non-zero retry count."""
    from src.llm.providers import make_discretionary_llm_client, origination_transient_retries

    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a", "GROQ_API_KEY": "x"}, clear=True):
        client = make_discretionary_llm_client()
    assert client is not None
    assert client._transient_retries == origination_transient_retries()
    assert client._transient_retries > 0


def test_make_discretionary_llm_client_none_when_nothing_available() -> None:
    """Can't happen in practice (Ollama is always attemptable) — documents the
    contract that make_*_llm_client() returns None rather than raising."""
    from src.llm.providers import make_discretionary_llm_client

    with patch("src.llm.providers._order_from_cfg", side_effect=lambda *_a, **_k: []):
        assert make_discretionary_llm_client() is None


# ── Independent G9 reviewers for LLM-originated strategies (gemini excluded) ──


_GATE_CFG = {
    "swing_g9": {"providers": ["groq", "ollama"]},
    "intraday_g9": {"providers": ["groq", "ollama"]},
}


def test_make_intraday_gate_llm_client_excludes_gemini() -> None:
    """llm_intraday_agent originates on the gemini-first chain, so its reviewer must
    not be gemini even when a gemini key is present."""
    from src.llm.providers import make_intraday_gate_llm_client

    with (
        patch("src.llm.providers._llm_cfg", return_value=_GATE_CFG),
        patch.dict(os.environ, {"GEMINI_API_KEY": "y", "GROQ_API_KEY": "x"}, clear=True),
    ):
        client = make_intraday_gate_llm_client()
    assert client is not None
    assert client._provider == LLMProvider.GROQ
    assert client._fallbacks == [LLMProvider.OLLAMA]


def test_swing_gate_client_unchanged_by_shared_helper() -> None:
    from src.llm.providers import make_swing_gate_llm_client

    with (
        patch("src.llm.providers._llm_cfg", return_value=_GATE_CFG),
        patch.dict(os.environ, {"GEMINI_API_KEY": "y", "GROQ_API_KEY": "x"}, clear=True),
    ):
        client = make_swing_gate_llm_client()
    assert client is not None
    assert client._provider == LLMProvider.GROQ
    assert client._fallbacks == [LLMProvider.OLLAMA]


# ── Cooldown circuit-breaker + OpenAI-compatible free tiers ──────────────────


@pytest.fixture(autouse=True)
def _clear_cooldowns() -> None:
    _prov._COOLDOWN_UNTIL.clear()


async def test_quota_error_fails_over_without_sleeping_and_sets_cooldown() -> None:
    c = LLMClient(LLMProvider.GROQ, fallbacks=[LLMProvider.CLOUDFLARE])
    env = {"GROQ_API_KEY": "x", "CLOUDFLARE_API_TOKEN": "y", "CLOUDFLARE_ACCOUNT_ID": "acc"}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("429 tokens per day"))),
        patch.object(c, "_openai_compat", new=AsyncMock(return_value="CF_OK")),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()) as sleep_mock,
    ):
        assert await c.complete("p") == "CF_OK"
    assert sleep_mock.await_count == 0  # next hop exists -> no backoff
    assert c.last_provider == LLMProvider.CLOUDFLARE
    assert _prov._cooling(LLMProvider.GROQ)


async def test_cooling_provider_is_skipped_on_next_call() -> None:
    c = LLMClient(LLMProvider.GROQ, fallbacks=[LLMProvider.CLOUDFLARE])
    groq = AsyncMock(return_value="GROQ_OK")
    _prov._COOLDOWN_UNTIL[LLMProvider.GROQ] = 1e18
    with (
        patch.dict(
            os.environ,
            {"GROQ_API_KEY": "x", "CLOUDFLARE_API_TOKEN": "y", "CLOUDFLARE_ACCOUNT_ID": "acc"},
            clear=True,
        ),
        patch.object(c, "_groq", new=groq),
        patch.object(c, "_openai_compat", new=AsyncMock(return_value="CF_OK")),
    ):
        assert await c.complete("p") == "CF_OK"
    groq.assert_not_awaited()


async def test_all_cooling_still_tries_chain_and_reraises_when_all_fail() -> None:
    c = LLMClient(LLMProvider.GROQ, fallbacks=[LLMProvider.CLOUDFLARE])
    for p in (LLMProvider.GROQ, LLMProvider.CLOUDFLARE):
        _prov._COOLDOWN_UNTIL[p] = 1e18
    with (
        patch.dict(
            os.environ,
            {"GROQ_API_KEY": "x", "CLOUDFLARE_API_TOKEN": "y", "CLOUDFLARE_ACCOUNT_ID": "acc"},
            clear=True,
        ),
        patch.object(c, "_groq", new=AsyncMock(side_effect=RuntimeError("429"))),
        patch.object(c, "_openai_compat", new=AsyncMock(side_effect=RuntimeError("429"))),
        patch("src.llm.providers.asyncio.sleep", new=AsyncMock()),
        pytest.raises(RuntimeError, match="429"),
    ):
        await c.complete("p")


def test_openai_compat_available_only_with_key_env() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert not _prov._provider_available(LLMProvider.CLOUDFLARE)
    with patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "k"}, clear=True):
        assert not _prov._provider_available(LLMProvider.CLOUDFLARE)  # account id missing too
    with patch.dict(
        os.environ, {"CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": "acc"}, clear=True
    ):
        assert _prov._provider_available(LLMProvider.CLOUDFLARE)


async def test_openai_compat_parses_completion_and_tokens() -> None:
    class _Resp:
        status = 200

        async def json(self, content_type: object = None) -> dict:
            return {
                "choices": [{"message": {"content": '{"ok": 1}'}}],
                "usage": {"total_tokens": 42},
            }

        async def __aenter__(self) -> _Resp:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _Sess(_Resp):
        def post(self, *_a: object, **_k: object) -> _Resp:
            return _Resp()

    c = LLMClient(LLMProvider.CLOUDFLARE)
    with (
        patch.dict(
            os.environ, {"CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": "acc"}, clear=True
        ),
        patch("aiohttp.ClientSession", new=lambda **_k: _Sess()),
    ):
        assert await c.complete("p") == '{"ok": 1}'
    assert c.last_tokens == 42


async def test_payment_required_gets_long_cooldown_and_fails_over() -> None:
    c = LLMClient(LLMProvider.GROQ, fallbacks=[LLMProvider.CLOUDFLARE])
    err = RuntimeError("groq_no_choices status=402: payment_required")
    with (
        patch.dict(
            os.environ,
            {"GROQ_API_KEY": "a", "CLOUDFLARE_API_TOKEN": "b", "CLOUDFLARE_ACCOUNT_ID": "acc"},
            clear=True,
        ),
        patch.object(c, "_groq", new=AsyncMock(side_effect=err)),
        patch.object(c, "_openai_compat", new=AsyncMock(return_value="CF_OK")),
    ):
        assert await c.complete("p") == "CF_OK"
    assert _prov._COOLDOWN_UNTIL[LLMProvider.GROQ] - _prov.time.monotonic() > 3000


@pytest.mark.asyncio  # type: ignore[misc]
async def test_hung_first_provider_fails_over_within_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-21: a stalled gemini (first in the discretionary chain, no effective
    timeout) ate the caller's whole budget, so llm_swing_agent's daily pass failed
    without groq ever being tried. A per-provider cap must raise so the chain moves on."""
    import asyncio

    from src.llm import providers as P

    monkeypatch.setattr(
        P,
        "_provider_timeout",
        lambda prov: 0.2 if prov == P.LLMProvider.GEMINI else None,
    )
    monkeypatch.setattr(P, "_provider_available", lambda prov: True)
    P._COOLDOWN_UNTIL.clear()
    client = P.LLMClient(P.LLMProvider.GEMINI, fallbacks=[P.LLMProvider.GROQ])

    async def _fake(self: object, prov: P.LLMProvider, *a: object) -> str:
        if prov == P.LLMProvider.GEMINI:
            await asyncio.sleep(5)
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake)
    out = await asyncio.wait_for(client.complete("x"), timeout=3)
    assert out == '{"ok": true}'
    assert client.last_provider == P.LLMProvider.GROQ
    assert P._cooling(P.LLMProvider.GEMINI)  # skipped next time instead of re-waited
    P._COOLDOWN_UNTIL.clear()
