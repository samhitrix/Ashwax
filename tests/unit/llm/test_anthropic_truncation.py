"""Anthropic replies cut off by the output-token limit must be detected and retried with more room
(2026-09-28: hidden "thinking" used ~all of max_tokens=3000, the visible answer was `{"th`, the code
treated it as success, then re-sent the IDENTICAL prompt -> two wasted calls + a duplicate alert)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from src.llm import providers as prov
from src.llm.providers import LLMClient
from src.strategies.llm_intraday.pipeline import ValidationLimits, run_intraday_reasoning


def _resp(text: str, stop_reason: str):
    blocks = [
        SimpleNamespace(type="thinking", thinking="..."),
        SimpleNamespace(type="text", text=text),
    ]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class _FakeAnthropic:
    def __init__(self, resp):
        self._resp = resp
        self.messages = SimpleNamespace(create=lambda **kw: self._resp)

    def with_options(self, **kw):
        return self


def _call(resp):
    with patch("anthropic.Anthropic", lambda **kw: _FakeAnthropic(resp)):
        return LLMClient._anthropic_one_sync("claude-sonnet-5", "p", "s", 3000, 30.0)


def test_a_reply_cut_off_at_max_tokens_is_an_error_not_a_success() -> None:
    with pytest.raises(RuntimeError) as ei:
        _call(_resp('{"th', "max_tokens"))
    assert "anthropic_truncated_response" in str(ei.value)
    assert prov._is_truncated(ei.value) is True  # so the retry loop escalates the token budget


def test_a_complete_reply_is_returned() -> None:
    assert _call(_resp('{"thesis":"x","ideas":[]}', "end_turn")) == '{"thesis":"x","ideas":[]}'


def test_gemini_truncation_is_still_recognised() -> None:
    assert prov._is_truncated(RuntimeError("gemini_truncated_response finish_reason=MAX_TOKENS"))
    assert not prov._is_truncated(RuntimeError("some other failure"))


# ── the pass must not retry with the identical prompt ────────────────────────────────────


class _ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self._provider = "anthropic"
        self.last_provider = None

    async def complete(self, prompt, system="", temperature=0.3, max_tokens=2048):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        return self.replies.pop(0)


def _run(client):
    feats = {"AXIS": {"ltp": 1000.0, "atr14_pct": 0.2, "chg_pct": -2.0, "vol_ratio20": 3.0}}
    return asyncio.run(
        run_intraday_reasoning(
            client,
            {"candidate_universe": [{"symbol": "AXIS", **feats["AXIS"]}]},
            [],
            [],
            feats,
            ValidationLimits(),
            max_ideas=2,
            timeout_secs=5.0,
            minutes_left=90,
        )
    )


def test_retry_after_an_unusable_reply_changes_the_prompt_and_raises_the_token_limit() -> None:
    good = json.dumps({"thesis": "nothing clean", "ideas": []})
    client = _ScriptedClient(['{"th', good])
    result = _run(client)
    assert result.decision == "no_ideas"  # the retry succeeded
    first, second = client.calls
    assert second["prompt"] != first["prompt"]  # never the identical prompt again
    assert "json" in second["prompt"].lower() and "only" in second["prompt"].lower()
    assert second["max_tokens"] > first["max_tokens"]


def test_first_attempt_gets_a_generous_output_budget_for_hidden_thinking() -> None:
    client = _ScriptedClient([json.dumps({"thesis": "x", "ideas": []})])
    _run(client)
    assert client.calls[0]["max_tokens"] >= 5000
