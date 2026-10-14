"""LLM provider registry — Gemini, Ollama, Anthropic, OpenAI."""

from __future__ import annotations

import asyncio
import json
import os
import time
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.infrastructure.logger import get_logger
from src.llm.token_ledger import LEDGER, LLMBudgetExhausted, daily_limit, estimate_tokens

_LLM_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "global.json"
_log = get_logger("llm")


@lru_cache(maxsize=1)
def _llm_cfg() -> dict[str, Any]:
    """`llm` block of config/global.json — single source of truth for model names,
    the Ollama base URL, and the trade-gate / background provider order."""
    try:
        return dict(json.loads(_LLM_CONFIG_PATH.read_text(encoding="utf-8")).get("llm", {}))
    except Exception:
        return {}


class LLMProvider(StrEnum):
    GEMINI = "gemini"
    GROQ = "groq"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    # OpenAI-compatible free tiers — endpoint/model/key env live in llm.openai_compat.
    CLOUDFLARE = "cloudflare"


_OPENAI_COMPAT = frozenset(
    {
        LLMProvider.CLOUDFLARE,
    }
)

# provider -> monotonic time before which it is skipped (quota/429 seen). Process-global
# so every LLMClient benefits: a provider that just said "quota exhausted" is not
# re-tried (and its backoff sleeps not paid) on each subsequent call inside a 3s gate budget.
_COOLDOWN_UNTIL: dict[LLMProvider, float] = {}
_COOLDOWN_MINUTE_S = 60.0
_COOLDOWN_DAILY_S = 3600.0  # daily-quota 429: re-probe hourly (resets at provider midnight)


# Per-MODEL cooldown for the gemini slot: free-tier quotas are per model, so one model hitting its
# 20/day cap (or being retired -> 404) must not sideline the other gemini models.
_MODEL_COOLDOWN_UNTIL: dict[str, float] = {}


def _model_cooling(model: str) -> bool:
    return _MODEL_COOLDOWN_UNTIL.get(model, 0.0) > time.monotonic()


def _mark_model_cooldown(model: str, exc: Exception) -> None:
    s = str(exc).lower()
    # retired / unknown model (404) or a per-day quota: re-probe hourly; a per-minute limit or
    # transient overload (503, timeout): one minute.
    long = (
        "404" in s
        or "not_found" in s
        or "no longer available" in s
        or "per day" in s
        or "perday" in s
        or "daily" in s
        or _is_payment_required(exc)
    )
    _MODEL_COOLDOWN_UNTIL[model] = time.monotonic() + (
        _COOLDOWN_DAILY_S if long else _COOLDOWN_MINUTE_S
    )


def gemini_models() -> list[str]:
    """Ordered gemini model list: `llm.gemini.models`, else the single `llm.gemini.model`."""
    block = _llm_cfg().get("gemini", {})
    models = block.get("models") if isinstance(block, dict) else None
    if isinstance(models, list) and models:
        return [str(m) for m in models]
    return [str(block.get("model", "gemini-2.0-flash"))] if isinstance(block, dict) else []


def anthropic_models() -> list[str]:
    """Ordered anthropic model list: `llm.anthropic.models`, else the single
    `llm.anthropic.model`. Same fallback-list convention as gemini_models()."""
    block = _llm_cfg().get("anthropic", {})
    models = block.get("models") if isinstance(block, dict) else None
    if isinstance(models, list) and models:
        return [str(m) for m in models]
    return [str(block.get("model", "claude-haiku-4-5"))] if isinstance(block, dict) else []


def _cooling(provider: LLMProvider) -> bool:
    return _COOLDOWN_UNTIL.get(provider, 0.0) > time.monotonic()


def _mark_cooldown(provider: LLMProvider, exc: Exception) -> None:
    s = str(exc).lower()
    daily = "per day" in s or "perday" in s or "daily" in s or _is_payment_required(exc)
    _COOLDOWN_UNTIL[provider] = time.monotonic() + (
        _COOLDOWN_DAILY_S if daily else _COOLDOWN_MINUTE_S
    )


class _RateLimiter:
    """Process-global throttle shared by every LLMClient so the 24/7 background
    loops (news classifier on each item, learning L7) cannot drain the provider
    free-tier quotas and 429 the whole system. Serializes calls to a minimum
    interval. Tunable via LLM_MAX_PER_MINUTE."""

    def __init__(self, max_per_minute: int = 20) -> None:
        self._min_interval = 60.0 / max(1, max_per_minute)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


_RATE_LIMITER = _RateLimiter(int(os.environ.get("LLM_MAX_PER_MINUTE", "20")))


def _guard_cfg() -> dict[str, Any]:
    """`llm.usage_guard` block of global.json: which purposes alert / are soft-budgeted, the
    oversized-prompt limits and the duplicate-call window."""
    g = _llm_cfg().get("usage_guard", {})
    return dict(g) if isinstance(g, dict) else {}


def _in_guard_list(key: str, purpose: str | None) -> bool:
    return purpose is not None and purpose in (_guard_cfg().get(key) or [])


def _max_prompt_chars(purpose: str | None) -> int:
    limits = _guard_cfg().get("max_prompt_chars") or {}
    if not isinstance(limits, dict):
        return 0
    return int(limits.get(purpose, limits.get("default", 0)) or 0)


def _alert_llm(message: str, *, source: str, purpose: str | None, tier: str = "CRITICAL") -> None:
    """CRITICAL alert (dashboard Alerts tab + Telegram) for a trade-path LLM problem.
    Only purposes listed in llm.usage_guard.alert_purposes alert (news/background are noisy)."""
    if not _in_guard_list("alert_purposes", purpose):
        return
    from src.infrastructure import operator_alerts

    minutes = float(_guard_cfg().get("failover_alert_dedupe_minutes", 10))
    operator_alerts.raise_alert(
        message,
        source=source,
        tier=tier,
        dedupe_seconds=minutes * 60.0,
        push_title=f"LLM {purpose}",
    )


def _provider_available(provider: LLMProvider) -> bool:
    """Cloud providers need an API key; Ollama is local and always attemptable."""
    if provider == LLMProvider.GEMINI:
        return bool(os.environ.get("GEMINI_API_KEY"))
    if provider == LLMProvider.GROQ:
        return bool(os.environ.get("GROQ_API_KEY"))
    if provider == LLMProvider.ANTHROPIC:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider in _OPENAI_COMPAT:
        cfg = _llm_cfg().get("openai_compat", {}).get(provider.value, {})
        needed = [cfg.get("key_env"), cfg.get("account_env")]
        return bool(cfg.get("key_env")) and all(os.environ.get(k) for k in needed if k)
    return provider == LLMProvider.OLLAMA


def _order_from_cfg(key: str, default: list[str]) -> list[LLMProvider]:
    """Provider chain for `llm.<key>` in global.json (e.g. trade_gate / background),
    mapped to LLMProvider and filtered to those actually available (API key / local)."""
    out: list[LLMProvider] = []
    for name in _llm_cfg().get(key, default):
        try:
            prov = LLMProvider(str(name).lower())
        except ValueError:
            continue
        if _provider_available(prov):
            out.append(prov)
    return out


def _is_truncated(exc: Exception) -> bool:
    """True when a provider returned a real, non-empty response that was cut off
    before it finished (see _gemini's finish_reason check) — most commonly a
    'thinking' model's internal reasoning tokens ate the visible-output budget
    before the JSON closed. Retrying with the SAME token budget usually hits the
    identical wall again (2026-09-16: silently cost llm_swing_agent and
    llm_intraday_agent real trade days, undetected because the old check only
    looked for empty/non-JSON text, not an incomplete one), so the caller
    escalates the budget on retry instead of just backing off."""
    s = str(exc)
    return "gemini_truncated_response" in s or "anthropic_truncated_response" in s


def _is_payment_required(exc: Exception) -> bool:
    """402 / no free credit on the account: retrying can't help until the user acts,
    so it gets the long cooldown rather than being re-hit on every call."""
    s = str(exc).lower()
    # Gemini's prepaid-credit error reads "402 RESOURCE_EXHAUSTED ... prepayment credits are
    # depleted" -- no "status=402" -- so match it explicitly, else it looked like a 1-minute
    # quota and was re-hit (3 models, ~9s) on every call.
    return (
        "status=402" in s
        or "payment_required" in s
        or ("402" in s and ("credits" in s or "prepayment" in s or "depleted" in s))
    )


def _is_rate_limited(exc: Exception) -> bool:
    """True when the provider error is a 429 / quota-exhausted, so we back off and
    retry the SAME provider before falling through to the next in the chain."""
    s = str(exc).lower()
    return "429" in s or "too many requests" in s or "resource_exhausted" in s or "rate limit" in s


def make_trade_llm_client(purpose: str | None = "intraday") -> LLMClient | None:
    """G9 gate client for RULE-GENERATED candidates (the DSL strategies — nothing an
    LLM originated), from `llm.trade_gate`. This chain carries the tightest budget in
    the system: trade_gate_timeout_seconds wraps the WHOLE chain, not each provider, so
    every hop must be fast enough to still leave room for a fallback. Providers are
    therefore ordered by MEASURED latency, and anthropic is pinned to its fastest model
    here via llm.chain_models (haiku, 763ms measured) rather than its global list, whose
    frontier model could never answer inside the cap. Returns None only if nothing is
    available (G9 then fails open, DSL-only) — see llm._model_roles in config/global.json
    for the full per-chain rationale."""
    order = _order_from_cfg("trade_gate", ["gemini", "groq", "ollama"])
    if not order:
        return None
    return LLMClient(
        order[0],
        fallbacks=order[1:],
        purpose=purpose,
        transient_retries=gate_transient_retries(),
        model_overrides=chain_models("trade_gate"),
    )


def make_discretionary_llm_client(purpose: str | None = None) -> LLMClient | None:
    """ORIGINATION client (src/strategies/llm_swing/, src/strategies/llm_intraday/) —
    the only place an LLM invents a trade from scratch (symbol/direction/entry/SL/target),
    a few calls a day, not the fast per-signal G9 check. Chain from `llm.discretionary`,
    ANTHROPIC-first: hardest task, lowest call volume, so the paid frontier model costs
    little here and the tier gap matters most. Whatever originates here MUST be excluded
    from the chains that review these ideas (llm.swing_g9 / llm.intraday_g9) — a model
    reviewing its own idea just agrees with itself. Returns None if nothing is available."""
    order = _order_from_cfg("discretionary", ["anthropic", "groq", "ollama"])
    if not order:
        return None
    return LLMClient(
        order[0],
        fallbacks=order[1:],
        purpose=purpose,
        transient_retries=origination_transient_retries(),
        model_overrides=chain_models("discretionary"),
    )


def make_swing_gate_llm_client() -> LLMClient | None:
    """G9 gate client for the swing path (`llm.swing_g9.providers` in global.json,
    GEMINI -> GROQ -> CLOUDFLARE). Deliberately EXCLUDES anthropic: llm_swing_agent
    originates its ideas on the anthropic-first `discretionary` chain, so putting
    anthropic here too would have it review its own morning idea and mostly agree
    with itself. A different provider is a real second opinion. Review is a bounded
    veto (REJECT / low-confidence only — it can never improve a trade's edge), so the
    fast tier is the right fit; the hard, alpha-determining call already happened at
    origination. Returns None when none available — the caller fails CLOSED for these
    strategies (an LLM-originated idea with no reviewer has no validation)."""
    return _gate_client_from_cfg("swing_g9", purpose="swing")


def make_intraday_gate_llm_client() -> LLMClient | None:
    """G9 gate client for LLM-originated INTRADAY strategies (`llm.intraday_g9.providers`,
    GEMINI -> GROQ -> CLOUDFLARE). Same reasoning as make_swing_gate_llm_client: the
    llm_intraday agent originates on the anthropic-first `discretionary` chain, so
    anthropic is excluded here to keep the review independent. Note this is a separate,
    longer-budget path from `trade_gate` (which reviews rule-generated DSL candidates on
    a much tighter timeout). None when nothing is available — the caller fails CLOSED
    for these strategies."""
    return _gate_client_from_cfg("intraday_g9", purpose="intraday")


def gate_transient_retries() -> int:
    """`llm.gate_transient_retries` (global.json): times a GATE client retries one provider on a
    transient error before failing over to the next. Default 3."""
    return _retries_cfg("gate_transient_retries", 3)


def origination_transient_retries() -> int:
    """`llm.origination_transient_retries`: retries for the ORIGINATION client
    (make_discretionary_llm_client). Higher than the gate count on purpose — it is the
    highest-value call in the system, fires only a few times a day, and runs on a 90s
    budget, so losing it to one flaky reply costs a whole day of ideas. Default 5."""
    return _retries_cfg("origination_transient_retries", 5)


def background_transient_retries() -> int:
    """`llm.background_transient_retries`: retries for the 24/7 news/learning client.
    Deliberately the LOWEST count — this loop runs on every news item, so aggressive
    retries burn free-tier quota the trade gates need (2026-09-15: news alone burned
    Groq's entire 200k/day). Default 1."""
    return _retries_cfg("background_transient_retries", 1)


def chain_models(chain: str) -> dict[str, list[str]]:
    """Per-chain model overrides from `llm.chain_models.<chain>` in global.json.

    Provider model lists (llm.<provider>.models) are global, but chains differ wildly in
    budget and job: origination has 90s and wants the frontier model, while trade_gate has
    3s and wants that provider's FASTEST model. Without an override the one global list has
    to serve both, and a slow model at its head locks the provider out of every tight gate
    (measured 2026-09-22: anthropic leads with sonnet at 6074ms, so it could never answer a
    3s gate — while its own haiku answered in 763ms, the fastest model in the whole stack).

    Returns {} when unset, i.e. every provider uses its normal list. Shape:
        "chain_models": {"trade_gate": {"anthropic": ["claude-haiku-4-5-20251001"]}}
    """
    block = _llm_cfg().get("chain_models", {})
    if not isinstance(block, dict):
        return {}
    per_chain = block.get(chain, {})
    if not isinstance(per_chain, dict):
        return {}
    out: dict[str, list[str]] = {}
    for prov, models in per_chain.items():
        if isinstance(models, str):
            out[str(prov).lower()] = [models]
        elif isinstance(models, list) and models:
            out[str(prov).lower()] = [str(m) for m in models]
    return out


def _retries_cfg(key: str, default: int) -> int:
    try:
        return max(0, int(_llm_cfg().get(key, default)))
    except (TypeError, ValueError):
        return default


def _provider_timeout(provider: LLMProvider) -> float | None:
    """`timeout_seconds` for one provider from llm.<provider> / llm.openai_compat.<provider>;
    None (uncapped) when unset, e.g. local ollama."""
    cfg = _llm_cfg()
    block = cfg.get(provider.value)
    if not isinstance(block, dict) or "timeout_seconds" not in block:
        block = (cfg.get("openai_compat") or {}).get(provider.value)
    try:
        return float(block["timeout_seconds"]) if isinstance(block, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def g9_chain_timeout(key: str, default: float) -> float:
    """Budget in seconds for the WHOLE provider chain of gate `llm.<key>` (swing_g9 /
    intraday_g9 / options_g9): `chain_timeout_seconds`. Sized to let a slow or
    rate-limited first provider fail over to the next instead of cancelling the
    chain (the shared 3s trade_gate cap did that)."""
    cfg = _llm_cfg().get(key, {})
    try:
        return (
            float(cfg.get("chain_timeout_seconds", default)) if isinstance(cfg, dict) else default
        )
    except (TypeError, ValueError):
        return default


def make_options_gate_llm_client() -> LLMClient | None:
    """G9 gate client for the options seller desk (`llm.options_g9.providers`
    in global.json, default GROQ -> OLLAMA). Unlike llm_swing_agent /
    llm_intraday_agent, the seller desk's structures are chosen by
    strategy_selector's regime/IVR rules, not an LLM, so there is no
    self-review conflict here — gemini is excluded anyway, for consistency
    with the other gate clients and to protect its free-tier quota for the
    strategies that actually depend on it. Returns None when nothing is
    available — the caller fails OPEN (`llm.options_g9.fail_closed`): the
    desk's own regime/IVR/Greeks gates are its independent basis, same as
    any DSL equity strategy."""
    return _gate_client_from_cfg("options_g9", purpose="options")


def _gate_client_from_cfg(key: str, purpose: str | None = None) -> LLMClient | None:
    """Build the client for one named G9 reviewer chain (swing_g9 / intraday_g9 /
    options_g9), honouring that chain's own llm.chain_models model overrides."""
    cfg = _llm_cfg().get(key, {})
    providers = cfg.get("providers", ["groq", "ollama"]) if isinstance(cfg, dict) else None
    out: list[LLMProvider] = []
    for name in providers or ["groq", "ollama"]:
        try:
            prov = LLMProvider(str(name).lower())
        except ValueError:
            continue
        if _provider_available(prov):
            out.append(prov)
    if not out:
        return None
    return LLMClient(
        out[0],
        fallbacks=out[1:],
        purpose=purpose,
        transient_retries=gate_transient_retries(),
        model_overrides=chain_models(key),
    )


def make_background_llm_client(purpose: str | None = "background") -> LLMClient | None:
    """Background-loop client (news classifier, learning L7): free providers only,
    NEVER a paid one (order from global.json `llm.background`, currently
    cloudflare -> groq). This is enforced here in code, not just by config
    convention, since a config edit alone silently drifts (2026-09-23 — the user's
    own words: "news should not be using any paid models"). Also never Gemini, to
    reserve its free-tier quota for the live G9 trade gate. Returns None when none
    available (caller degrades: news → keyword, learning → skip)."""
    order = _order_from_cfg("background", ["groq", "ollama"])
    # Belt-and-braces: whatever the config says, a PAID provider can never end up in
    # the background chain. Filtered here rather than trusted from config, because
    # config drift is exactly how a future edit could reintroduce one silently.
    order = [p for p in order if p not in (LLMProvider.ANTHROPIC, LLMProvider.OPENAI)]
    if not order:
        return None
    return LLMClient(
        order[0],
        fallbacks=order[1:],
        purpose=purpose,
        transient_retries=background_transient_retries(),
        model_overrides=chain_models("background"),
    )


class LLMClient:
    def __init__(
        self,
        provider: str | LLMProvider = LLMProvider.GEMINI,
        fallbacks: list[LLMProvider] | None = None,
        purpose: str | None = None,
        transient_retries: int = 0,
        model_overrides: dict[str, list[str]] | None = None,
    ) -> None:
        self._provider = LLMProvider(provider) if isinstance(provider, str) else provider
        # Per-CHAIN model choice, overriding the provider's global llm.<provider>.model(s).
        # The provider model list is global, but chains have very different budgets and
        # jobs: origination wants the frontier model and has 90s, while a 3s review gate
        # wants the fastest model that provider has. Without this, one list has to serve
        # both and the slow model at its head locks the provider out of every tight gate.
        # Shape: {"anthropic": ["claude-haiku-4-5-..."]}; absent provider = its normal list.
        self._model_overrides = {k.lower(): list(v) for k, v in (model_overrides or {}).items()}
        # Budget bucket (config/global.json llm.token_budgets.daily_tokens.<purpose>);
        # None = unmetered. See src/llm/token_ledger.py.
        self._purpose = purpose
        # Gate clients retry a provider this many times on a transient error (5xx, network,
        # bad/non-JSON reply) before failing over to the next one. 0 = fail over at once.
        self._transient_retries = max(0, transient_retries)
        # Explicit ordered fallback chain (e.g. trade gate: GEMINI→GROQ→OLLAMA).
        # When None, the legacy single-step GROQ↔GEMINI fallback is used so
        # news/background clients keep their existing behaviour.
        self._fallbacks = fallbacks
        # Side-channel populated by the LAST successful complete() call — which
        # provider actually answered (never inferred from self._provider, the
        # configured PRIMARY: a fallback chain can succeed on its 2nd/3rd hop)
        # and, best-effort, how many tokens it billed. Additive: existing
        # callers that never read these two attributes are unaffected. Added
        # 2026-09-15 so a caller can attribute spend to the specific provider
        # a shared daily quota (e.g. Groq's) actually needs protecting from —
        # see src/news/token_budget.py.
        self.last_provider: LLMProvider | None = None
        self.last_tokens: int | None = None

    async def complete(
        self, prompt: str, system: str = "", temperature: float = 0.3, max_tokens: int = 2048
    ) -> str:
        # Global throttle: keeps total LLM throughput under the free-tier rate so
        # neither GROQ (primary) nor GEMINI (fallback) gets exhausted.
        if self._purpose is not None:
            left = await LEDGER.remaining(self._purpose)
            if left is not None and left <= 0:
                LEDGER.note_exhausted(self._purpose)
                spent = await LEDGER.spent(self._purpose)
                limit = daily_limit(self._purpose)
                if _in_guard_list("soft_budget_purposes", self._purpose):
                    # Trade-path purposes (the agents' whole reason to exist) are NEVER
                    # blocked by a spending counter: alert loudly and carry on.
                    _log.warning(
                        "llm_budget_exceeded_soft purpose=%s spent=%d limit=%s -- call proceeds "
                        "(soft cap; raise llm.token_budgets.daily_tokens.%s if intended)",
                        self._purpose,
                        spent,
                        limit,
                        self._purpose,
                    )
                    _alert_llm(
                        f"LLM token budget for '{self._purpose}' exceeded: spent {spent} of "
                        f"{limit} tokens today. Soft cap - calls still go through. Check for "
                        "oversized prompts / duplicate calls, or raise "
                        f"llm.token_budgets.daily_tokens.{self._purpose}.",
                        source=f"llm_budget_soft:{self._purpose}",
                        purpose=self._purpose,
                        tier="ALERT",
                    )
                else:
                    raise LLMBudgetExhausted(
                        f"llm token budget exhausted purpose={self._purpose} spent={spent} "
                        f"limit={limit} (llm.token_budgets.daily_tokens.{self._purpose}); "
                        "refused BEFORE any provider was tried"
                    )
            cached = await self._cached_duplicate(prompt, system)
            if cached is not None:
                return cached
            await self._guard_prompt(prompt, system)
        result = await self._complete_metered(prompt, system, temperature, max_tokens)
        if self._purpose is not None and self.last_provider != LLMProvider.OLLAMA:
            await LEDGER.add(
                self._purpose, self.last_tokens or estimate_tokens(prompt, system, result)
            )
        if self._purpose is not None:
            await self._remember_answer(prompt, system, result)
        return result

    def _dup_key(self, prompt: str, system: str) -> str:
        import hashlib

        return hashlib.sha256(f"{self._purpose}|{system}|{prompt}".encode()).hexdigest()[:20]

    async def _cached_duplicate(self, prompt: str, system: str) -> str | None:
        """Serve an IDENTICAL prompt from the earlier answer instead of calling the LLM again
        (2026-10-06: the options desk re-sent the same ~454-token review every 5 min).
        A repeat within llm.usage_guard.duplicate_retry_grace_seconds (default 120) is a
        caller's deliberate retry of a malformed/missing reply and goes through.
        duplicate_window_seconds=0 or reuse_duplicates=false disables reuse."""
        g = _guard_cfg()
        window = int(g.get("duplicate_window_seconds", 900))
        if window <= 0 or not bool(g.get("reuse_duplicates", True)):
            return None
        hit = await LEDGER.cached_response(self._dup_key(prompt, system), window)
        if hit is None:
            return None
        age, payload = hit
        if age < float(g.get("duplicate_retry_grace_seconds", 120)):
            return None  # a retry: let it through
        import json as _json

        d = _json.loads(payload)
        prov = d.get("provider")
        self.last_provider = LLMProvider(prov) if prov else None
        self.last_tokens = 0
        _log.info(
            "llm_duplicate_served_from_cache purpose=%s age_s=%.0f provider=%s -- no LLM call",
            self._purpose,
            age,
            prov,
        )
        return str(d["result"])

    async def _remember_answer(self, prompt: str, system: str, result: str) -> None:
        import json as _json

        window = int(_guard_cfg().get("duplicate_window_seconds", 900))
        if window <= 0:
            return
        prov = getattr(self.last_provider, "value", None)
        await LEDGER.store_response(
            self._dup_key(prompt, system),
            _json.dumps({"result": result, "provider": prov}),
            window,
        )

    async def _guard_prompt(self, prompt: str, system: str) -> None:
        """Visibility + tripwires on every purposed call (never blocks): one INFO line with
        the real size, an alert if the prompt exceeds llm.usage_guard.max_prompt_chars (a
        150k-char swing prompt cost ~42k tokens per call before this existed), and a
        duplicate detector for an IDENTICAL prompt re-sent inside the window (detected, not
        suppressed: callers legitimately retry a malformed reply with the same prompt)."""
        import hashlib

        chars = len(prompt) + len(system)
        est = estimate_tokens(system, prompt)
        _log.info(
            "llm_call_plan purpose=%s prompt_chars=%d system_chars=%d est_tokens=%d",
            self._purpose,
            len(prompt),
            len(system),
            est,
        )
        cap = _max_prompt_chars(self._purpose)
        if cap and chars > cap:
            _log.error(
                "llm_prompt_oversized purpose=%s chars=%d limit=%d est_tokens=%d",
                self._purpose,
                chars,
                cap,
                est,
            )
            _alert_llm(
                f"LLM prompt for '{self._purpose}' is oversized: {chars:,} chars (~{est:,} "
                f"tokens) vs limit {cap:,}. Something is stuffing the prompt - the call still "
                "went out. Fix the prompt builder.",
                source=f"llm_prompt_oversized:{self._purpose}",
                purpose=self._purpose,
            )
        window = int(_guard_cfg().get("duplicate_window_seconds", 900))
        if window > 0 and _in_guard_list("alert_purposes", self._purpose):
            digest = hashlib.sha256(f"{self._purpose}|{system}|{prompt}".encode()).hexdigest()[:20]
            grace = int(_guard_cfg().get("duplicate_retry_grace_seconds", 120))
            # Recorded on every call: a repeat inside the grace window is the caller's own
            # retry of a timed-out/malformed reply (evaluate_trade_gated), not a loop. Only the
            # FIRST such repeat is excused (``:retry_used``, window-long): a 3rd identical send,
            # or a restart loop re-sending inside the grace, still alerts.
            in_grace = grace > 0 and await LEDGER.seen_recently(f"{digest}:grace", grace)
            seen = await LEDGER.seen_recently(digest, window)
            is_retry = (
                seen
                and in_grace
                and not await LEDGER.seen_recently(f"{digest}:retry_used", window)
            )
            if seen and is_retry:
                _log.info(
                    "llm_retry_after_no_answer purpose=%s est_tokens=%d grace_s=%d -- identical "
                    "prompt re-sent as a deliberate retry",
                    self._purpose,
                    est,
                    grace,
                )
            elif seen:
                _log.warning(
                    "llm_duplicate_call purpose=%s est_tokens=%d window_s=%d -- identical prompt "
                    "already sent",
                    self._purpose,
                    est,
                    window,
                )
                _alert_llm(
                    f"DUPLICATE LLM call for '{self._purpose}': an identical prompt (~{est:,} "
                    f"tokens) was already sent in the last {window // 60} min. Wasted tokens - "
                    "check for restarts or a retry loop.",
                    source=f"llm_duplicate:{self._purpose}",
                    purpose=self._purpose,
                    tier="ALERT",
                )

    async def _complete_metered(
        self, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        await _RATE_LIMITER.acquire()
        self.last_provider = None
        self.last_tokens = None
        if self._fallbacks is not None:
            return await self._complete_chain(prompt, system, temperature, max_tokens)
        try:
            result = await self._dispatch_with_retry(
                self._provider, prompt, system, temperature, max_tokens
            )
            self.last_provider = self._provider
            return result
        except Exception:
            # Resilience: if the primary provider errors (rate-limit / quota exhausted /
            # no choices), fall back to the OTHER configured cloud provider in EITHER
            # direction (GROQ↔GEMINI) so the LLM gate isn't silenced. Re-raise if none.
            alt = self._fallback_provider()
            if alt is not None:
                result = await self._dispatch(alt, prompt, system, temperature, max_tokens)
                self.last_provider = alt
                return result
            raise

    async def _complete_chain(
        self, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        """Try the primary then each configured fallback in order, skipping cloud
        providers whose API key is absent (Ollama is local → always attemptable).
        Returns the first success; re-raises the last error only if all fail."""
        last_exc: Exception | None = None
        chain = [p for p in (self._provider, *(self._fallbacks or [])) if _provider_available(p)]
        # Providers that recently hit quota are skipped outright — unless that would
        # leave nothing, in which case try them all (the cooldown may be stale).
        live = [p for p in chain if not _cooling(p)]
        for i, prov in enumerate(live or chain):
            is_last = i == len(live or chain) - 1
            t0 = time.perf_counter()
            try:
                result = await self._dispatch_with_retry(
                    prov,
                    prompt,
                    system,
                    temperature,
                    max_tokens,
                    max_retries=2 if is_last else 1,
                    backoff_429=is_last,
                )
                self.last_provider = prov
                _log.info(
                    "llm_call ok purpose=%s provider=%s latency_ms=%d tokens=%s position=%d/%d",
                    self._purpose,
                    prov.value,
                    int((time.perf_counter() - t0) * 1000),
                    self.last_tokens,
                    i + 1,
                    len(live or chain),
                )
                return result
            except Exception as exc:  # noqa: BLE001 — try the next provider in the chain
                last_exc = exc
                _log.warning(
                    "llm_call failed purpose=%s provider=%s latency_ms=%d err=%s%s",
                    self._purpose,
                    prov.value,
                    int((time.perf_counter() - t0) * 1000),
                    f"{type(exc).__name__}:{str(exc)[:160]}",
                    "" if is_last else " -> next provider",
                )
                # A provider that HUNG (per-provider cap hit) is skipped for the short
                # cooldown too, so the next call doesn't re-wait the full cap on it.
                if (
                    _is_rate_limited(exc)
                    or _is_payment_required(exc)
                    or isinstance(exc, TimeoutError)
                ):
                    _mark_cooldown(prov, exc)
                _alert_llm(
                    f"LLM provider '{prov.value}' FAILED for '{self._purpose}': "
                    f"{type(exc).__name__}: {str(exc)[:160]}. "
                    + (
                        "Failing over to the next provider."
                        if not is_last
                        else "No providers left in the chain."
                    ),
                    source=f"llm_failover:{self._purpose}:{prov.value}",
                    purpose=self._purpose,
                )
        if last_exc is not None:
            _alert_llm(
                f"ALL LLM providers failed for '{self._purpose}': "
                f"{type(last_exc).__name__}: {str(last_exc)[:160]}. No answer was obtained.",
                source=f"llm_all_failed:{self._purpose}",
                purpose=self._purpose,
            )
            raise last_exc
        raise RuntimeError("no LLM provider available in chain")

    async def _dispatch_with_retry(
        self,
        provider: LLMProvider,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int,
        max_retries: int = 2,
        backoff_429: bool = True,
    ) -> str:
        """Dispatch to one provider, retrying before giving up (caller then tries
        the next provider in the chain): exponential backoff on a 429 / quota-
        exhausted error, or an escalated token budget on a truncated-response
        error (see _is_truncated) — the two failure shapes need opposite fixes,
        so both are handled in this one loop rather than the caller guessing
        which happened."""
        delay = 1.0
        tokens = max_tokens
        attempt = 0
        transient_left = self._transient_retries
        while True:
            try:
                return await self._dispatch(provider, prompt, system, temperature, tokens)
            except Exception as exc:
                if _is_rate_limited(exc):
                    if attempt >= max_retries or not backoff_429:
                        raise  # a next provider exists — fail over now, don't sleep
                    attempt += 1
                    await asyncio.sleep(delay)
                    delay *= 2.0
                    continue
                if _is_truncated(exc):
                    if attempt >= max_retries:
                        raise
                    attempt += 1
                    tokens = min(int(tokens * 1.8), 8000)
                    continue
                # Transient failure (5xx / network / non-JSON reply): retry this provider once
                # before moving on. Not for quota/payment (retrying cannot help) and not for a
                # HUNG provider (TimeoutError) — it would just hang again and burn the chain
                # budget; fail over instead.
                if (
                    transient_left > 0
                    and not _is_payment_required(exc)
                    and not isinstance(exc, TimeoutError)
                ):
                    transient_left -= 1
                    _log.warning(
                        "llm_provider_retry provider=%s purpose=%s err=%s",
                        provider.value,
                        self._purpose,
                        str(exc)[:160],
                    )
                    await asyncio.sleep(0.5)
                    continue
                raise

    def _fallback_provider(self) -> LLMProvider | None:
        """The other configured cloud provider to try when the primary fails."""
        if self._provider != LLMProvider.GROQ and os.environ.get("GROQ_API_KEY"):
            return LLMProvider.GROQ
        if self._provider != LLMProvider.GEMINI and os.environ.get("GEMINI_API_KEY"):
            return LLMProvider.GEMINI
        return None

    async def _dispatch(
        self, provider: LLMProvider, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        """One provider call, hard-capped at that provider's `timeout_seconds`
        (config/global.json llm.<provider>, or llm.openai_compat.<provider>) so a HUNG
        provider raises and the chain fails over to the next one. Before 2026-09-21
        gemini's configured 10s was never applied (the SDK call runs in a thread with
        no timeout), so a stalled gemini — first in the discretionary chain — ate the
        caller's whole 30s budget and llm_swing_agent's daily pass failed without
        groq/cloudflare ever being tried. No cap = ollama (slow local model).

        DEBUG-logs the full request and response around every provider call, one
        place for the whole codebase rather than each strategy logging its own raw
        text (or, as with trade_gate/background/G9 chains, not at all). No logic
        here changes: the timeout/dispatch path is identical, this only wraps it.
        Truncated at 4000 chars — long enough to read a real prompt/response, short
        enough that a verbose origination pass can't blow up llm.log.

        Also charges the per-PROVIDER ledger (D1/D2, 2026-09-23) here, once per
        ATTEMPT rather than once per successful complete() call: an estimate is
        recorded before dispatch ("charge on send"), so a killed or failed attempt —
        today's 8 swing calls that never returned cost real Anthropic tokens and were
        recorded as zero — is no longer free. On a successful return, the estimate is
        corrected to the provider's REAL usage where it reports one (groq/cloudflare;
        anthropic/gemini/ollama do not, so their figures stay estimates). This is a
        separate dimension from the per-purpose budget above (which still gates
        whether a call is allowed at all) -- purely observability, never enforcement,
        because Gemini's actual failure mode is a per-REQUEST free-tier limit that no
        purpose bucket can represent."""
        cap = _provider_timeout(provider)
        _t0 = time.perf_counter()
        _log.debug(
            "llm_asked purpose=%s provider=%s system=%r prompt=%r",
            self._purpose,
            provider.value,
            system[:4000],
            prompt[:4000],
        )
        self.last_tokens = None  # cleared per ATTEMPT, not per complete() call — see
        # class docstring: "the LAST successful call". Without this, a provider that
        # never reports usage (anthropic/gemini) would inherit a stale figure left
        # over from an earlier successful hop to a DIFFERENT provider in the same
        # fallback chain.
        _pre_tokens = estimate_tokens(system, prompt)
        _log.info(
            "llm_call_sent purpose=%s provider=%s est_tokens=%d",
            self._purpose,
            provider.value,
            _pre_tokens,
        )
        try:
            await LEDGER.add_provider_tokens(provider.value, _pre_tokens)
            await LEDGER.add_provider_request(provider.value)
        except Exception:  # noqa: BLE001 — observability must never block a real call
            pass
        try:
            if cap is None:
                result = await self._dispatch_uncapped(
                    provider, prompt, system, temperature, max_tokens
                )
            else:
                result = await asyncio.wait_for(
                    self._dispatch_uncapped(provider, prompt, system, temperature, max_tokens),
                    timeout=cap,
                )
        except Exception as exc:
            _log.debug(
                "llm_got purpose=%s provider=%s latency_ms=%d error=%s",
                self._purpose,
                provider.value,
                int((time.perf_counter() - _t0) * 1000),
                f"{type(exc).__name__}:{str(exc)[:200]}",
            )
            raise
        _log.debug(
            "llm_got purpose=%s provider=%s latency_ms=%d response=%r",
            self._purpose,
            provider.value,
            int((time.perf_counter() - _t0) * 1000),
            result[:4000],
        )
        _real_tokens = self.last_tokens
        if _real_tokens is not None and _real_tokens != _pre_tokens:
            try:
                await LEDGER.add_provider_tokens(provider.value, _real_tokens - _pre_tokens)
            except Exception:  # noqa: BLE001 — observability must never break a real call
                pass
        return result

    async def _dispatch_uncapped(
        self, provider: LLMProvider, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        if provider == LLMProvider.GEMINI:
            return await self._gemini(prompt, system, temperature, max_tokens)
        if provider == LLMProvider.GROQ:
            return await self._groq(prompt, system, temperature, max_tokens)
        if provider == LLMProvider.OLLAMA:
            return await self._ollama(prompt, system, temperature, max_tokens)
        if provider == LLMProvider.ANTHROPIC:
            return await self._anthropic(prompt, system, temperature, max_tokens)
        if provider in _OPENAI_COMPAT:
            return await self._openai_compat(provider, prompt, system, temperature, max_tokens)
        raise NotImplementedError(f"Provider {provider} not implemented")

    def _models_for(self, provider: LLMProvider, default: list[str]) -> list[str]:
        """This chain's model list for `provider` — the per-chain override when one is
        configured (llm.chain_models.<chain>.<provider>), else the provider's global list."""
        return self._model_overrides.get(provider.value) or default

    async def _gemini(self, prompt: str, system: str, temperature: float, max_tokens: int) -> str:
        """Try each configured gemini model in order (config llm.gemini.models, or this
        chain's llm.chain_models override); a model that is over quota, retired, overloaded
        or slow is put on its own cooldown and the NEXT model takes the call, so gemini
        stays usable while any one of them works."""
        models = self._models_for(LLMProvider.GEMINI, gemini_models())
        live = [m for m in models if not _model_cooling(m)] or models
        last_exc: Exception | None = None
        for model in live:
            try:
                return await self._gemini_one(model, prompt, system, temperature, max_tokens)
            except Exception as exc:  # noqa: BLE001 — fail over to the next model
                last_exc = exc
                _mark_model_cooldown(model, exc)
                _log.warning(
                    "llm_gemini_model_failed model=%s err=%s%s",
                    model,
                    f"{type(exc).__name__}:{str(exc)[:140]}",
                    " -> next model" if model != live[-1] else "",
                )
                _alert_llm(
                    f"LLM model '{model}' (gemini) FAILED for '{self._purpose}': "
                    f"{type(exc).__name__}: {str(exc)[:140]}"
                    + (" Trying the next model." if model != live[-1] else ""),
                    source=f"llm_model_failed:{self._purpose}:{model}",
                    purpose=self._purpose,
                )
        if last_exc is None:
            raise RuntimeError("no gemini model configured (llm.gemini.models)")
        raise last_exc

    async def _gemini_one(
        self, model: str, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        import asyncio as _asyncio

        from google import genai as _genai

        # One attempt, and the provider cap as the SDK's own timeout. By default the SDK sleeps
        # and retries on 429 (honouring "retry in 36s"), which turned a 0.5s "free-tier quota
        # exceeded" into a silent 25s+ stall and a misleading TIMEOUT (found 2026-09-21).
        _cap = float(_llm_cfg().get("gemini", {}).get("model_timeout_seconds", 12))
        client = _genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY", ""),
            http_options=_genai.types.HttpOptions(
                timeout=int(_cap * 1000) if _cap else None,
                retry_options=_genai.types.HttpRetryOptions(attempts=1),
            ),
        )
        # Loosen categorized safety blocks for this internal, system-to-system,
        # numeric risk-scoring call (no end user ever sees the prompt or reply).
        # Note: Gemini has no "financial advice" harm category, so this alone won't
        # stop a values-driven refusal like "I cannot answer that" — see the system
        # instruction in trade_evaluator.py for that half of the fix.
        _safety = [
            _genai.types.SafetySetting(
                category=c, threshold=_genai.types.HarmBlockThreshold.BLOCK_ONLY_HIGH
            )
            for c in (
                _genai.types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                _genai.types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                _genai.types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                _genai.types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            )
        ]
        response = await _asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=prompt,
            config=_genai.types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                system_instruction=system or None,
                safety_settings=_safety,
            ),
        )
        text = response.text
        candidates = getattr(response, "candidates", None) or []
        finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish_name = str(getattr(finish_reason, "name", finish_reason))
        if text and "{" in text and finish_name not in ("STOP", "None"):
            # Real, non-empty, JSON-shaped text that still didn't finish cleanly
            # (most commonly MAX_TOKENS): this model spends internal 'thinking'
            # tokens from the SAME max_output_tokens budget as the visible reply
            # (confirmed live 2026-09-16: usage_metadata.thoughts_token_count),
            # so a verbose reasoning pass can cut the JSON off mid-string. The
            # old check below only caught EMPTY or NON-JSON text, so this shape
            # slipped through as a false "success" and both llm_swing_agent
            # and llm_intraday_agent silently lost real trade days to it —
            # _dispatch_with_retry retries this with an escalated token budget.
            raise RuntimeError(
                f"gemini_truncated_response finish_reason={finish_name} "
                f"len={len(text)} text={text[:80]!r}"
            )
        if not text or "{" not in text:
            # Two failure shapes seen in practice, neither raises on its own:
            # (1) safety-blocked / empty-candidate → text is None/"" with no exception;
            # (2) Gemini ignores the "respond with JSON only" instruction and replies
            #     in plain English instead (observed: raw='I cannot answer that') —
            #     non-empty, so it looked like success, but has no '{' at all.
            # Both get treated as SUCCESS by _complete_chain, which then never falls
            # through to Groq/Ollama. Raise so the chain actually tries them.
            raise RuntimeError(
                f"gemini_non_json_response finish_reason={finish_name} text={text[:80]!r}"
            )
        return text  # type: ignore[no-any-return]

    async def _groq(self, prompt: str, system: str, temperature: float, max_tokens: int) -> str:
        import aiohttp

        headers = {
            "Authorization": f"Bearer {os.environ.get('GROQ_API_KEY', '')}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        async with (
            aiohttp.ClientSession() as sess,
            sess.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json={
                    "model": self._models_for(
                        LLMProvider.GROQ,
                        [str(_llm_cfg().get("groq", {}).get("model", "llama-3.3-70b-versatile"))],
                    )[0],
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            ) as resp,
        ):
            data = await resp.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            # Groq returned an error envelope (rate limit, auth, bad model …)
            # instead of completions. Surface the real cause, not KeyError('choices').
            err = ""
            if isinstance(data, dict):
                err = str(data.get("error") or data)[:200]
            raise RuntimeError(f"groq_no_choices: {err}")
        # Real usage from Groq's OpenAI-compatible response — this is what a
        # daily-token-budget caller (src/news/token_budget.py) needs to attribute
        # spend correctly; best-effort (missing usage just leaves last_tokens None).
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            self.last_tokens = int(usage["total_tokens"])
        return choices[0]["message"]["content"]  # type: ignore[no-any-return]

    async def _openai_compat(
        self,
        provider: LLMProvider,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Generic OpenAI-style /chat/completions call for the free tiers registered
        under llm.openai_compat (base_url / model / key_env per provider)."""
        import aiohttp

        cfg = _llm_cfg().get("openai_compat", {}).get(provider.value, {})
        headers = {
            "Authorization": f"Bearer {os.environ.get(cfg.get('key_env', ''), '')}",
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        if cfg.get("account_env"):  # e.g. Cloudflare: account id is part of the URL path
            base_url = base_url.replace("{account_id}", os.environ.get(cfg["account_env"], ""))
        timeout = aiohttp.ClientTimeout(total=float(cfg.get("timeout_seconds", 10)))
        async with (
            aiohttp.ClientSession(timeout=timeout) as sess,
            sess.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json={
                    "model": self._models_for(provider, [str(cfg.get("model", ""))])[0],
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            ) as resp,
        ):
            status = resp.status
            data = await resp.json(content_type=None)
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            # Error envelope — keep "429"/quota wording in the message so
            # _is_rate_limited / _mark_cooldown see it.
            err = str(data.get("error") or data)[:200] if isinstance(data, dict) else ""
            raise RuntimeError(f"{provider.value}_no_choices status={status}: {err}")
        text = choices[0]["message"]["content"]
        if not text or "{" not in text:
            raise RuntimeError(f"{provider.value}_non_json_response text={str(text)[:80]!r}")
        usage = data.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            self.last_tokens = int(usage["total_tokens"])
        return text  # type: ignore[no-any-return]

    async def _anthropic(
        self, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        """Try each configured anthropic model in order (config llm.anthropic.models) —
        same fail-over-to-the-next-model pattern as _gemini(): a model that's
        overloaded, retired or refuses gets its own cooldown and the next model
        takes the call, so anthropic stays usable while any one of them works.
        Honours this chain's llm.chain_models override when one is set — that is
        what lets a 3s review gate use haiku while origination keeps sonnet."""
        models = self._models_for(LLMProvider.ANTHROPIC, anthropic_models())
        live = [m for m in models if not _model_cooling(m)] or models
        last_exc: Exception | None = None
        for model in live:
            try:
                return await self._anthropic_one(model, prompt, system, temperature, max_tokens)
            except Exception as exc:  # noqa: BLE001 — fail over to the next model
                last_exc = exc
                _mark_model_cooldown(model, exc)
                _log.warning(
                    "llm_anthropic_model_failed model=%s err=%s%s",
                    model,
                    f"{type(exc).__name__}:{str(exc)[:140]}",
                    " -> next model" if model != live[-1] else "",
                )
                _alert_llm(
                    f"LLM model '{model}' (anthropic) FAILED for '{self._purpose}': "
                    f"{type(exc).__name__}: {str(exc)[:140]}"
                    + (" Trying the next model." if model != live[-1] else ""),
                    source=f"llm_model_failed:{self._purpose}:{model}",
                    purpose=self._purpose,
                )
        if last_exc is None:
            raise RuntimeError("no anthropic model configured (llm.anthropic.models)")
        raise last_exc

    async def _anthropic_one(
        self, model: str, prompt: str, system: str, temperature: float, max_tokens: int
    ) -> str:
        """Runs the SDK call in a worker thread via asyncio.to_thread (2026-09-23), never
        awaited directly on the event loop. Evidence: on 2026-09-23 the swing origination
        pass's anthropic call stalled the supervisor heartbeat dead for 140+ seconds --
        past this call's own 60-75s timeout budget, meaning the timeout itself could not
        fire because the loop that was supposed to run it was not scheduling anything.
        The launcher's hang watchdog then killed the whole process, wiping every
        in-memory throttle (the daily-run guard, intraday's pass counters, provider
        cooldowns) and causing a restart loop that re-asked the same question every ~7
        minutes. Same defect class already fixed once for blocking Kite calls
        (to_thread-wrapped then) — a synchronous SDK call cannot starve the loop if it
        never runs on the loop's own thread, whatever it does internally. `temperature`
        is accepted for signature parity with _gemini/_groq/_ollama (uniform _dispatch
        call shape) but the installed anthropic SDK's Messages.create() has no
        temperature parameter — nothing to forward."""
        del temperature
        _acfg = _llm_cfg().get("anthropic", {})
        _cap = float(_acfg.get("model_timeout_seconds", _acfg.get("timeout_seconds", 8)))
        return await asyncio.to_thread(
            self._anthropic_one_sync, model, prompt, system, max_tokens, _cap
        )

    @staticmethod
    def _anthropic_one_sync(
        model: str, prompt: str, system: str, max_tokens: int, cap: float
    ) -> str:
        """The actual blocking SDK call. Runs on a worker thread via asyncio.to_thread
        in _anthropic_one above — never on the event loop thread. Uses the synchronous
        Anthropic client deliberately, not AsyncAnthropic: the whole point of this split
        is that IF the SDK's async path is what was blocking the loop (unproven, but
        consistent with the observed heartbeat stall), running it here instead sidesteps
        the question entirely rather than depending on the async client's internals
        being fixed correctly."""
        from anthropic import Anthropic

        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "")).with_options(
            timeout=cap
        )
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"anthropic_refusal stop_reason={resp.stop_reason}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "max_tokens":
            # The model spends hidden "thinking" tokens out of the SAME max_tokens budget, so a long
            # deliberation can leave the visible answer cut off mid-word (2026-09-28: `{"th`).
            # That is NOT a success: raise so _dispatch_with_retry re-asks with a bigger budget
            # instead of the caller retrying the identical prompt with the identical limit.
            raise RuntimeError(
                f"anthropic_truncated_response stop_reason=max_tokens max_tokens={max_tokens} "
                f"visible_chars={len(text)}"
            )
        if not text or "{" not in text:
            # Same failure shape as the Gemini/Ollama empty-or-non-JSON response — must raise,
            # not return, or _complete_chain treats it as success and never tries the next hop.
            raise RuntimeError(
                f"anthropic_non_json_response stop_reason={resp.stop_reason} text={text[:80]!r}"
            )
        return text

    async def _ollama(self, prompt: str, system: str, temperature: float, max_tokens: int) -> str:
        import aiohttp

        _ocfg = _llm_cfg().get("ollama", {})
        _base = str(_ocfg.get("base_url", "http://localhost:11434")).rstrip("/")
        _model = _ocfg.get("model") or os.environ.get("OLLAMA_MODEL", "llama3")
        async with (
            aiohttp.ClientSession() as sess,
            sess.post(
                f"{_base}/api/generate",
                json={
                    "model": _model,
                    "prompt": prompt,
                    "system": system,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
            ) as resp,
        ):
            data = await resp.json()
        if not isinstance(data, dict) or "error" in data:
            raise RuntimeError(f"ollama_error: {str(data)[:200]}")
        text = data.get("response", "")
        if not text:
            # Same failure shape as the Gemini empty-candidate bug (see _gemini) —
            # a 200 with no error but blank content must raise, not look like success,
            # or _complete_chain treats it as done and the caller silently gets "".
            raise RuntimeError(f"ollama_empty_response: done_reason={data.get('done_reason')!r}")
        return text  # type: ignore[no-any-return]
