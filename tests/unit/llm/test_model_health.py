"""LLM health check (morning/evening/startup) + the gate's retry-once-before-failover."""

from __future__ import annotations

import asyncio
from datetime import time
from pathlib import Path
from typing import Any

import pytest
from src.llm import model_health as mh
from src.llm import providers as P
from src.market import sessions


# ── gate retry: one retry on the SAME provider before moving on ────────────────
def _client(retries: int) -> P.LLMClient:
    return P.LLMClient(
        P.LLMProvider.GROQ, fallbacks=[P.LLMProvider.OLLAMA], transient_retries=retries
    )


@pytest.fixture(autouse=True)
def _providers_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(P, "_provider_available", lambda prov: True)
    monkeypatch.setattr(P, "_provider_timeout", lambda prov: None)
    P._COOLDOWN_UNTIL.clear()


@pytest.mark.asyncio  # type: ignore[misc]
async def test_transient_error_is_retried_once_on_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake(self: object, prov: P.LLMProvider, *a: object) -> str:
        calls.append(prov.value)
        if len(calls) == 1:
            raise RuntimeError("groq_no_choices status=503: upstream hiccup")
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake)
    client = _client(retries=1)
    assert await client.complete("x") == '{"ok": true}'
    assert calls == ["groq", "groq"]  # retried groq, never reached ollama
    assert client.last_provider == P.LLMProvider.GROQ


@pytest.mark.asyncio  # type: ignore[misc]
async def test_without_retry_a_transient_error_fails_over_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _fake(self: object, prov: P.LLMProvider, *a: object) -> str:
        calls.append(prov.value)
        if prov == P.LLMProvider.GROQ:
            raise RuntimeError("groq_no_choices status=503")
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake)
    assert await _client(retries=0).complete("x") == '{"ok": true}'
    assert calls == ["groq", "ollama"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_hung_provider_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fake(self: object, prov: P.LLMProvider, *a: object) -> str:
        calls.append(prov.value)
        if prov == P.LLMProvider.GROQ:
            raise TimeoutError
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake)
    assert await _client(retries=1).complete("x") == '{"ok": true}'
    assert calls == ["groq", "ollama"]  # a hang would just hang again — fail over


@pytest.mark.asyncio  # type: ignore[misc]
async def test_quota_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _fake(self: object, prov: P.LLMProvider, *a: object) -> str:
        calls.append(prov.value)
        if prov == P.LLMProvider.GROQ:
            raise RuntimeError("status=402 payment_required")
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake)
    await _client(retries=1).complete("x")
    assert calls == ["groq", "ollama"]


# ── health check ───────────────────────────────────────────────────────────────
@pytest.fixture()
def health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"providers": {}, "chains": {}, "alerts": []}
    monkeypatch.setattr(mh, "is_trading_day", lambda d: True)  # real clock may be a holiday
    monkeypatch.setattr(mh, "_HEALTH_PATH", tmp_path / "llm_model_health.json")
    monkeypatch.setattr(mh, "_configured_provider_names", lambda: list(state["providers"]))

    async def _prov(name: str, model: str | None = None) -> dict[str, Any]:
        return {"provider": name, "model": "m", **state["providers"][name]}

    async def _chain(name: str, factory: Any, *_: Any) -> dict[str, Any]:
        return {"chain": name, **state["chains"][name]}

    monkeypatch.setattr(mh, "check_provider", _prov)
    monkeypatch.setattr(mh, "check_chain", _chain)
    monkeypatch.setattr(mh, "_chains", lambda: {n: None for n in state["chains"]})
    monkeypatch.setattr(
        "src.notifications.notify", lambda kind, **kw: state["alerts"].append(kw), raising=False
    )
    return state


@pytest.mark.asyncio  # type: ignore[misc]
async def test_all_ok_is_silent_and_persisted(health: dict[str, Any]) -> None:
    health["providers"] = {"groq": {"status": "OK"}}
    health["chains"] = {"swing_g9": {"status": "OK", "answered_by": "groq"}}
    res = await mh.run_health_check("morning")
    assert res["overall"] == "OK" and health["alerts"] == []
    assert mh.load_health()["overall"] == "OK"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_failure_on_non_trading_day_sends_no_telegram(
    health: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mh, "is_trading_day", lambda d: False)
    health["providers"] = {"groq": {"status": "ERROR", "error": "down"}}
    health["chains"] = {"swing_g9": {"status": "FAIL", "error": "all providers failed"}}
    assert (await mh.run_health_check("startup"))["overall"] == "FAIL"
    assert health["alerts"] == []  # still persisted for the dashboard, just not pushed


def test_scheduled_probes_skipped_on_non_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    marked: list[str] = []

    async def _run(trigger: str) -> dict[str, Any]:
        ran.append(trigger)
        return {}

    monkeypatch.setattr(mh, "run_health_check", _run)
    monkeypatch.setattr(mh, "mark_slot_run", lambda slot, day: marked.append(slot))
    monkeypatch.setattr(mh, "is_trading_day", lambda d: False)
    monkeypatch.setattr(mh, "load_health", lambda: {"checked_at": None, "slot_runs": {}})
    monkeypatch.setattr("src.llm.call_caps.startup_probe_due", lambda *a: False)
    monkeypatch.setattr(sessions, "schedule_time", lambda n: time(0, 0))  # every slot is due

    async def _go() -> None:
        stop = asyncio.Event()
        orig = asyncio.wait_for

        async def _fast(aw: Any, timeout: float) -> Any:  # noqa: ASYNC109
            if timeout == 45.0:
                aw.close()
                raise TimeoutError
            stop.set()
            return await orig(aw, 0.01)

        monkeypatch.setattr(asyncio, "wait_for", _fast)
        await mh.llm_health_loop(stop)

    asyncio.run(_go())
    assert ran == []  # no startup (recent), no morning/evening probe on a holiday


@pytest.mark.asyncio  # type: ignore[misc]
async def test_dead_provider_alerts_degraded_and_recovery_alerts_once(
    health: dict[str, Any],
) -> None:
    health["providers"] = {
        "gemini": {"status": "TIMEOUT", "error": "hung"},
        "groq": {"status": "OK"},
    }
    health["chains"] = {"swing_g9": {"status": "OK", "answered_by": "groq"}}
    res = await mh.run_health_check("evening")
    assert res["overall"] == "DEGRADED"
    assert len(health["alerts"]) == 1 and "gemini" in health["alerts"][0]["failures"][0]

    health["providers"]["gemini"] = {"status": "OK"}  # fixed
    res2 = await mh.run_health_check("morning")
    assert res2["overall"] == "OK"
    assert len(health["alerts"]) == 2 and health["alerts"][1]["overall"] == "OK"  # recovery

    await mh.run_health_check("evening")  # still fine -> no further alert
    assert len(health["alerts"]) == 2


@pytest.mark.asyncio  # type: ignore[misc]
async def test_chain_with_no_answer_is_fail(health: dict[str, Any]) -> None:
    health["providers"] = {"groq": {"status": "ERROR", "error": "down"}}
    health["chains"] = {"swing_g9": {"status": "FAIL", "error": "all providers failed"}}
    assert (await mh.run_health_check("startup"))["overall"] == "FAIL"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_history_is_kept_and_slot_marker_persists(health: dict[str, Any]) -> None:
    health["providers"] = {"groq": {"status": "OK"}}
    health["chains"] = {"swing_g9": {"status": "OK", "answered_by": "groq"}}
    await mh.run_health_check("startup")
    mh.mark_slot_run("morning", "2026-09-22")
    await mh.run_health_check("evening")
    data = mh.load_health()
    assert [h["trigger"] for h in data["history"]] == ["startup", "evening"]
    assert data["slot_runs"]["morning"] == "2026-09-22"  # survives the next run


def test_health_loop_runs_startup_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    async def _run(trigger: str) -> dict[str, Any]:
        ran.append(trigger)
        return {}

    monkeypatch.setattr(mh, "run_health_check", _run)
    monkeypatch.setattr(mh, "mark_slot_run", lambda *a: None)
    monkeypatch.setattr(mh, "load_health", lambda: {"slot_runs": {"morning": "x", "evening": "x"}})

    async def _go() -> None:
        stop = asyncio.Event()
        orig = asyncio.wait_for

        async def _fast(aw: Any, timeout: float) -> Any:  # noqa: ASYNC109
            if timeout == 45.0:
                aw.close()  # never awaited; pretend the 45s startup delay elapsed
                raise TimeoutError
            stop.set()  # first 60s wait -> end the loop
            return await orig(aw, 0.01)

        monkeypatch.setattr(asyncio, "wait_for", _fast)
        await mh.llm_health_loop(stop)

    asyncio.run(_go())
    assert ran[0] == "startup"


@pytest.mark.asyncio  # type: ignore[misc]
async def test_chain_with_healthy_primary_makes_no_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-tier quotas are tiny (gemini: 20 requests/day) — a chain whose primary was just
    probed OK must be reported OK without spending another call."""
    calls: list[str] = []

    async def _boom(self: object, *a: object, **k: object) -> str:
        calls.append("called")
        return "{}"

    monkeypatch.setattr(P.LLMClient, "complete", _boom)
    monkeypatch.setattr(P, "_provider_available", lambda prov: True)
    row = await mh.check_chain(
        "swing_g9",
        lambda: P.LLMClient(P.LLMProvider.GROQ, fallbacks=[P.LLMProvider.OLLAMA]),
        {"groq": {"status": "OK"}},
    )
    assert row["status"] == "OK" and row["answered_by"] == "groq" and calls == []

    # primary failed -> the chain IS called for real, to verify failover
    async def _ok(self: P.LLMClient, *a: object, **k: object) -> str:
        self.last_provider = P.LLMProvider.OLLAMA
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "complete", _ok)
    row2 = await mh.check_chain(
        "swing_g9",
        lambda: P.LLMClient(P.LLMProvider.GROQ, fallbacks=[P.LLMProvider.OLLAMA]),
        {"groq": {"status": "ERROR"}},
    )
    assert row2["status"] == "DEGRADED" and row2["answered_by"] == "ollama"


def test_per_day_quota_error_gets_the_daily_cooldown() -> None:
    P._COOLDOWN_UNTIL.clear()
    exc = RuntimeError(
        "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    )
    P._mark_cooldown(P.LLMProvider.GEMINI, exc)
    import time as _t

    assert (
        P._COOLDOWN_UNTIL[P.LLMProvider.GEMINI] - _t.monotonic() > 3000
    )  # hourly re-probe, not 60s
    P._COOLDOWN_UNTIL.clear()


# ── gemini model list: each model fails over to the next, with its own cooldown ─────────
@pytest.fixture()
def gemini_models(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    models = ["m-lite", "m-preview", "m-latest"]
    monkeypatch.setattr(P, "gemini_models", lambda: models)
    P._MODEL_COOLDOWN_UNTIL.clear()
    return models


@pytest.mark.asyncio  # type: ignore[misc]
async def test_gemini_falls_through_models_until_one_answers(
    gemini_models: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str] = []

    async def _one(self: object, model: str, *a: object) -> str:
        tried.append(model)
        if model == "m-lite":
            raise RuntimeError("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel")
        if model == "m-preview":
            raise RuntimeError("503 UNAVAILABLE high demand")
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_gemini_one", _one)
    client = P.LLMClient(P.LLMProvider.GEMINI)
    assert await client._gemini("p", "", 0.0, 100) == '{"ok": true}'
    assert tried == ["m-lite", "m-preview", "m-latest"]
    assert P._model_cooling("m-lite") and P._model_cooling("m-preview")


@pytest.mark.asyncio  # type: ignore[misc]
async def test_cooled_model_is_skipped_on_the_next_call(
    gemini_models: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    P._mark_model_cooldown("m-lite", RuntimeError("404 NOT_FOUND no longer available"))
    tried: list[str] = []

    async def _one(self: object, model: str, *a: object) -> str:
        tried.append(model)
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_gemini_one", _one)
    await P.LLMClient(P.LLMProvider.GEMINI)._gemini("p", "", 0.0, 100)
    assert tried == ["m-preview"]  # retired/quota'd model not re-tried; hourly re-probe only
    import time as _t

    assert P._MODEL_COOLDOWN_UNTIL["m-lite"] - _t.monotonic() > 3000


@pytest.mark.asyncio  # type: ignore[misc]
async def test_all_gemini_models_failing_raises_the_last_error(
    gemini_models: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _one(self: object, model: str, *a: object) -> str:
        raise RuntimeError(f"{model} down")

    monkeypatch.setattr(P.LLMClient, "_gemini_one", _one)
    with pytest.raises(RuntimeError, match="m-latest down"):
        await P.LLMClient(P.LLMProvider.GEMINI)._gemini("p", "", 0.0, 100)


def test_shipped_config_lists_three_gemini_models() -> None:
    models = P.gemini_models()
    assert len(models) == 3 and len(set(models)) == 3


# ── write_token_usage_snapshot (D5, 2026-09-23) ────────────────────────────────
# logs/llm_token_usage.json is written here (orchestrator process, where LEDGER has
# a real Redis backend) so GET /api/llm_tokens (a different process) has something
# to read -- the same bridge llm_model_health.json already uses, for the same reason.


@pytest.mark.asyncio  # type: ignore[misc]
async def test_write_token_usage_snapshot_writes_provider_and_purpose_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    from src.llm.token_ledger import LEDGER

    monkeypatch.setattr(mh, "_TOKEN_USAGE_PATH", tmp_path / "llm_token_usage.json")
    await LEDGER.add_provider_tokens("gemini", 500)
    await LEDGER.add_provider_request("gemini")

    await mh.write_token_usage_snapshot()

    payload = json.loads((tmp_path / "llm_token_usage.json").read_text(encoding="utf-8"))
    assert payload["checked_at"] is not None
    assert payload["providers"]["gemini"]["tokens"] >= 500
    assert payload["providers"]["gemini"]["requests"] >= 1
    assert "swing" in payload["purposes"]


@pytest.mark.asyncio  # type: ignore[misc]
async def test_check_provider_charges_the_provider_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-23 regression guard: check_provider calls a provider directly
    (_dispatch_uncapped / _anthropic_one / _gemini_one), one layer below
    LLMClient._dispatch where charging normally happens -- a real API request was
    going out here and landing as 0 tok / 0 req on the dashboard for every provider
    except gemini (whose count only came from an unrelated gate-chain check)."""
    from src.llm.token_ledger import LEDGER

    monkeypatch.setattr(P, "_provider_available", lambda prov: True)

    async def _fake_dispatch_uncapped(self: object, prov: object, *a: object) -> str:
        return '{"ok": true}'

    monkeypatch.setattr(P.LLMClient, "_dispatch_uncapped", _fake_dispatch_uncapped)

    before = await LEDGER.provider_requests_made("groq")
    await mh.check_provider("groq")
    after = await LEDGER.provider_requests_made("groq")

    assert after == before + 1
    assert await LEDGER.provider_tokens_spent("groq") > 0


@pytest.mark.asyncio  # type: ignore[misc]
async def test_write_token_usage_snapshot_never_raises_on_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: a write failure must not break the health loop it piggybacks on."""
    monkeypatch.setattr(mh, "_TOKEN_USAGE_PATH", Path("/nonexistent/dir/file.json"))
    await mh.write_token_usage_snapshot()  # must not raise
