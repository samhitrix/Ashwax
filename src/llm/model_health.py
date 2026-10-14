"""
LLM model health check — proves every configured provider and every gate chain actually
answers, twice a day (config/market_hours.json schedules.llm_health_morning / _evening) and
once at startup.

Why: a provider can silently rot (model renamed/retired, key revoked, quota gone, endpoint
hanging — gemini hung for weeks unnoticed, 2026-09-21) and the first symptom used to be a
skipped trade or an "LLM ORIGINATION FAILED" alert. This finds it first.

Per run it:
  * calls each provider directly with a tiny JSON prompt (valid model? key? reachable?),
  * calls each named gate chain end-to-end (which provider actually answers, and is it the
    primary?),
  * logs every result to llm.log (one line each + a summary),
  * writes logs/llm_model_health.json (latest result + recent history) for the dashboard,
  * sends a Telegram alert when anything is not OK, and a recovery message when it clears.

The probe calls are unmetered (they bypass the per-purpose token budgets) — ~100 tokens each.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger
from src.llm import providers as P
from src.market.calendar import is_trading_day

_log = get_logger("llm")
_IST = ZoneInfo("Asia/Kolkata")
_HEALTH_PATH = Path(__file__).parent.parent.parent / "logs" / "llm_model_health.json"
_TOKEN_USAGE_PATH = Path(__file__).parent.parent.parent / "logs" / "llm_token_usage.json"
_HISTORY = 30
_PROMPT = 'Respond with exactly this JSON and nothing else: {"ok": true}'
_SYSTEM = "You are a health probe. Reply with JSON only."
_OLLAMA_CAP_S = 120.0  # local model is slow to load; only used for this probe
_CHAIN_CAP_S = 150.0

OK, DEGRADED, FAIL = "OK", "DEGRADED", "FAIL"


def _configured_provider_names() -> list[str]:
    """Every provider named in any chain in global.json (llm.*), in first-seen order."""
    cfg = P._llm_cfg()
    names: list[str] = []
    for key in ("trade_gate", "background", "discretionary"):
        names += [str(n).lower() for n in cfg.get(key, []) or []]
    for key in ("swing_g9", "intraday_g9", "options_g9"):
        block = cfg.get(key, {})
        if isinstance(block, dict):
            names += [str(n).lower() for n in block.get("providers", []) or []]
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(n, None)
    return list(seen)


def _model_of(prov: P.LLMProvider) -> str:
    cfg = P._llm_cfg()
    block = cfg.get(prov.value)
    if isinstance(block, dict) and block.get("model"):
        return str(block["model"])
    compat = (cfg.get("openai_compat") or {}).get(prov.value)
    return str(compat.get("model", "")) if isinstance(compat, dict) else ""


def _classify(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "TIMEOUT"
    if P._is_rate_limited(exc) or P._is_payment_required(exc):
        return "QUOTA"
    return "ERROR"


# Providers whose config carries a `models` fallback list (gemini_models() /
# anthropic_models()) get one health probe PER model, via this per-model dispatch
# method on LLMClient -- every other provider gets one probe on its single
# configured model via the normal chain dispatch.
_PER_MODEL_DISPATCH: dict[str, str] = {"gemini": "_gemini_one", "anthropic": "_anthropic_one"}


async def check_provider(name: str, model: str | None = None) -> dict[str, Any]:
    """One direct call to one provider (for gemini/anthropic: to one specific model
    of its list). status: OK | NO_KEY | TIMEOUT | QUOTA | BAD_REPLY | ERROR."""
    row: dict[str, Any] = {"provider": name, "model": "", "status": "ERROR", "latency_ms": None}
    try:
        prov = P.LLMProvider(name)
    except ValueError:
        row.update(status="ERROR", error=f"unknown provider {name!r}")
        return row
    row["model"] = model or _model_of(prov)
    if not P._provider_available(prov):
        row.update(status="NO_KEY", error="API key / account not configured")
        return row
    client = P.LLMClient(prov)  # no purpose -> unmetered (the PURPOSE budgets, not
    # the per-provider dashboard counters below -- see that section's comment).
    cap = (
        float(P._llm_cfg().get(name, {}).get("model_timeout_seconds", 12))
        if model
        else (P._provider_timeout(prov) or _OLLAMA_CAP_S)
    )
    # Charge the per-PROVIDER ledger here explicitly (2026-09-23): this probe calls a
    # SPECIFIC model directly (_anthropic_one / _gemini_one / _dispatch_uncapped), one
    # layer below LLMClient._dispatch -- the chokepoint every ordinary complete() call
    # goes through and where this same charging already happens for those. A real
    # Anthropic/Groq/Cloudflare API request was going out here and landing as
    # "0 tok / 0 req" on the dashboard, understating the real per-provider load
    # (Gemini's own probes are the same cost, and no dashboard reader could tell any
    # of the three request-metered providers were ever actually called). Still
    # observability only -- this never gates whether the probe itself can run.
    from src.llm.token_ledger import LEDGER, estimate_tokens

    _pre_tokens = estimate_tokens(_SYSTEM, _PROMPT)
    try:
        await LEDGER.add_provider_tokens(name, _pre_tokens)
        await LEDGER.add_provider_request(name)
    except Exception:  # noqa: BLE001 — observability must never block the probe
        pass
    t0 = time.perf_counter()
    try:
        # A provider's own cap can be tight for a cold start; give the probe 2x headroom.
        if model:
            dispatch = getattr(client, _PER_MODEL_DISPATCH[name])
            call = dispatch(model, _PROMPT, _SYSTEM, 0.0, 200)
        else:
            call = client._dispatch_uncapped(prov, _PROMPT, _SYSTEM, 0.0, 200)
        text = await asyncio.wait_for(call, timeout=cap * 2)
        row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        try:
            json.loads(text[text.index("{") : text.rindex("}") + 1])
            row["status"] = "OK"
        except (ValueError, json.JSONDecodeError):
            row.update(status="BAD_REPLY", error=f"non-JSON reply: {text[:80]!r}")
        row["tokens"] = client.last_tokens
        if client.last_tokens is not None and client.last_tokens != _pre_tokens:
            try:
                await LEDGER.add_provider_tokens(name, client.last_tokens - _pre_tokens)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — every failure mode is a health finding
        row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        row.update(status=_classify(exc), error=f"{type(exc).__name__}: {str(exc)[:200]}")
    return row


def _chains() -> dict[str, Any]:
    return {
        "trade_gate": P.make_trade_llm_client,
        "swing_g9": P.make_swing_gate_llm_client,
        "intraday_g9": P.make_intraday_gate_llm_client,
        "options_g9": P.make_options_gate_llm_client,
        "discretionary": P.make_discretionary_llm_client,
        "background": P.make_background_llm_client,
    }


async def check_chain(
    name: str, factory: Any, probed: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Which provider answers through one named chain, and is it the primary.

    When the chain's primary provider was just probed OK the chain is known-good and NO extra
    call is made (free-tier quotas are tiny — gemini allows 20 requests/day — so probes must
    not burn them). Only a chain whose primary failed is called for real, to verify failover."""
    row: dict[str, Any] = {"chain": name, "status": "FAIL", "answered_by": None}
    client = factory()
    if client is None:
        row["error"] = "no provider available for this chain"
        return row
    client._purpose = None  # probe must not spend the purpose's token share
    primary = client._provider.value
    if probed and probed.get(primary, {}).get("status") == "OK":
        row.update(status=OK, answered_by=primary, primary=primary, latency_ms=None)
        row["note"] = "primary provider probe OK (no extra call)"
        return row
    t0 = time.perf_counter()
    try:
        text = await asyncio.wait_for(
            client.complete(_PROMPT, system=_SYSTEM, temperature=0.0, max_tokens=200),
            timeout=_CHAIN_CAP_S,
        )
        row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        answered = client.last_provider.value if client.last_provider else None
        row["answered_by"], row["primary"] = answered, primary
        json.loads(text[text.index("{") : text.rindex("}") + 1])
        row["status"] = OK if answered == primary else DEGRADED
        if row["status"] == DEGRADED:
            row["error"] = f"primary {primary} failed; answered by {answered}"
    except Exception as exc:  # noqa: BLE001
        row["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return row


def load_health() -> dict[str, Any]:
    """Latest persisted result ({} when none) — read by the dashboard API and the scheduler."""
    try:
        return dict(json.loads(_HEALTH_PATH.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _write(result: dict[str, Any], previous: dict[str, Any]) -> None:
    history = list(previous.get("history", []))
    history.append(
        {
            "checked_at": result["checked_at"],
            "trigger": result["trigger"],
            "overall": result["overall"],
            "failures": result["failures"],
        }
    )
    result["history"] = history[-_HISTORY:]
    result["slot_runs"] = dict(previous.get("slot_runs", {}))
    tmp = _HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    tmp.replace(_HEALTH_PATH)


async def run_health_check(trigger: str) -> dict[str, Any]:
    """Run every probe, log, persist and alert. trigger: morning | evening | startup | manual."""
    previous = load_health()
    providers: list[dict[str, Any]] = []
    for n in _configured_provider_names():
        if n == "gemini":  # one probe per model in llm.gemini.models — every model checked
            providers += [await check_provider(n, m) for m in P.gemini_models()]
        elif n == "anthropic":  # one probe per model in llm.anthropic.models, same reasoning
            providers += [await check_provider(n, m) for m in P.anthropic_models()]
        else:
            providers.append(await check_provider(n))
    by_name: dict[str, dict[str, Any]] = {}
    for r in providers:  # provider is usable if ANY of its rows is OK
        if r["provider"] not in by_name or r["status"] == "OK":
            by_name[r["provider"]] = r
    chains = [await check_chain(n, f, by_name) for n, f in _chains().items()]

    failures: list[str] = []
    for r in providers:
        if r["status"] != "OK":
            failures.append(
                f"provider {r['provider']} ({r['model'] or '?'}): {r['status']} — {r.get('error', '')}"
            )
    for r in chains:
        if r["status"] != OK:
            failures.append(f"chain {r['chain']}: {r['status']} — {r.get('error', '')}")
    if any(r["status"] == FAIL for r in chains):
        overall = FAIL
    elif failures:
        overall = DEGRADED
    else:
        overall = OK

    for r in providers:
        (_log.info if r["status"] == "OK" else _log.warning)(
            "llm_health provider=%s model=%s status=%s latency_ms=%s tokens=%s%s",
            r["provider"],
            r["model"],
            r["status"],
            r.get("latency_ms"),
            r.get("tokens"),
            f" err={r['error']}" if r.get("error") else "",
        )
    for r in chains:
        (_log.info if r["status"] == OK else _log.warning)(
            "llm_health_chain name=%s status=%s answered_by=%s primary=%s latency_ms=%s%s",
            r["chain"],
            r["status"],
            r.get("answered_by"),
            r.get("primary"),
            r.get("latency_ms"),
            f" err={r['error']}" if r.get("error") else "",
        )
    _log.info(
        "llm_health_summary trigger=%s overall=%s providers_ok=%d/%d chains_ok=%d/%d",
        trigger,
        overall,
        sum(1 for r in providers if r["status"] == "OK"),
        len(providers),
        sum(1 for r in chains if r["status"] == OK),
        len(chains),
    )

    result: dict[str, Any] = {
        "checked_at": datetime.now(_IST).isoformat(timespec="seconds"),
        "trigger": trigger,
        "overall": overall,
        "failures": failures,
        "providers": providers,
        "chains": chains,
    }
    _write(result, previous)

    prev_overall = previous.get("overall", OK)
    # No Telegram on weekends/holidays (the one holiday notice is sent elsewhere).
    if (overall != OK or prev_overall != OK) and is_trading_day(datetime.now(_IST).date()):
        try:
            from src.notifications import EventKind, notify

            notify(
                EventKind.LLM_HEALTH,
                is_paper=True,
                trigger=trigger,
                overall=overall,
                failures=failures,
            )
        except Exception:  # notify() must never break the check
            pass
    return result


def mark_slot_run(slot: str, day: str) -> None:
    """Persist that `slot` (morning/evening) already ran on `day` so a restart doesn't repeat it."""
    data = load_health()
    if not data:
        return
    data.setdefault("slot_runs", {})[slot] = day
    tmp = _HEALTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_HEALTH_PATH)


async def write_token_usage_snapshot() -> None:
    """logs/llm_token_usage.json — per-provider and per-purpose LLM spend, for the
    dashboard's Agent Status / System Health cards (D5, 2026-09-23).

    Written here (from this process's llm_health_loop, orchestrator only) rather than
    read live from the API server: TokenLedger is Redis-backed but only the
    orchestrator process ever calls LEDGER.configure(redis_client) — the API server's
    own LEDGER import has self._redis = None and would show all zeros. This mirrors
    the exact bridge llm_model_health.json already uses for the same reason (see
    module docstring): orchestrator writes, GET /api/llm_tokens reads via _read_log.

    Piggybacks on this loop's existing 60s cadence rather than adding a new timer —
    the existing interval is already sized for "how fresh does LLM dashboard data need
    to be," which is exactly this data too."""
    from src.llm.token_ledger import LEDGER

    try:
        payload = {
            "checked_at": datetime.now(_IST).isoformat(),
            "providers": await LEDGER.provider_snapshot(),
            "purposes": await LEDGER.purpose_snapshot(),
        }
        tmp = _TOKEN_USAGE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(_TOKEN_USAGE_PATH)
    except Exception as exc:  # noqa: BLE001 — best-effort, never break the health loop
        _log.debug("llm_token_usage_write_failed err=%s", str(exc)[:120])


async def llm_health_loop(stop: asyncio.Event) -> None:
    """Startup probe (after a short delay), then the morning and evening probes daily."""
    from src.market import sessions

    try:
        await asyncio.wait_for(stop.wait(), timeout=45.0)
        return
    except TimeoutError:
        pass
    # Skip the boot probe when a probe already completed recently (config
    # llm.health_probe.startup_min_interval_minutes). It used to run on EVERY boot and
    # hit all providers -- 32 times on 2026-09-24 because the orchestrator restarted ~30x.
    from src.llm.call_caps import startup_probe_due

    _min_interval = float(
        P._llm_cfg().get("health_probe", {}).get("startup_min_interval_minutes", 240)
    )
    if startup_probe_due(load_health().get("checked_at"), datetime.now(_IST), _min_interval):
        try:
            await run_health_check("startup")
        except Exception as exc:  # noqa: BLE001
            _log.error("llm_health_startup_failed err=%s", exc)
    else:
        _log.info(
            "llm_health_startup_skipped reason=recent_check min_interval_min=%.0f", _min_interval
        )
    await write_token_usage_snapshot()  # so the dashboard isn't empty for the first 60s
    # The startup probe IS today's status for every slot time that has already passed — don't
    # replay the morning/evening probes back-to-back after a late-day restart.
    boot = datetime.now(_IST)
    for slot, sched in (("morning", "llm_health_morning"), ("evening", "llm_health_evening")):
        if boot.time() >= sessions.schedule_time(sched):
            mark_slot_run(slot, boot.date().isoformat())
    while not stop.is_set():
        now = datetime.now(_IST)
        today = now.date().isoformat()
        runs = load_health().get("slot_runs", {})
        for slot, sched in (
            ("morning", "llm_health_morning"),
            ("evening", "llm_health_evening"),
        ):
            if now.time() >= sessions.schedule_time(sched) and runs.get(slot) != today:
                if not is_trading_day(now.date()):
                    # Weekend / NSE holiday: no trading to gate, so no provider pings.
                    mark_slot_run(slot, today)
                    continue
                try:
                    await run_health_check(slot)
                except Exception as exc:  # noqa: BLE001
                    _log.error("llm_health_%s_failed err=%s", slot, exc)
                mark_slot_run(slot, today)
                break
        # Piggybacks on this loop's existing 60s wake cadence (see
        # write_token_usage_snapshot docstring) rather than a new timer.
        await write_token_usage_snapshot()
        try:
            await asyncio.wait_for(stop.wait(), timeout=60.0)
        except TimeoutError:
            pass
