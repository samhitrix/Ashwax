"""
Per-purpose daily LLM token budgets (config/global.json llm.token_budgets).

Every metered LLMClient carries a `purpose` (swing, intraday, options, news_classify,
background). Before a call the client asks `remaining(purpose)`; after it, `add(purpose,
tokens)`. A purpose that has spent its reserved share is REFUSED (LLMBudgetExhausted) — it
can never borrow another purpose's tokens, which is the point: on 2026-09-15 news
classification burned Groq's whole 200k/day and silently starved the trade-critical G9
reviews. Callers already treat any LLM exception as "no verdict" (gated strategies skip the
trade, news falls back to keywords), so exhaustion degrades the same way an outage does.

Limits are re-read from config on every call, so raising/lowering a purpose's tokens in
global.json takes effect without a restart. Counts persist in Redis when available (survive
a restart) with an in-memory fallback. Local ollama spends nothing and is not counted.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger

_CFG_PATH = Path(__file__).parent.parent.parent / "config" / "global.json"
_IST = ZoneInfo("Asia/Kolkata")
_KEY_PREFIX = "llm:tokens"
_PROVIDER_TOKENS_PREFIX = "llm:tokens:provider"
_PROVIDER_REQUESTS_PREFIX = "llm:requests:provider"
_log = get_logger("llm")

# Providers tracked for the per-provider dashboard view (D1, 2026-09-23). Not tied to
# any budget/limit -- this dimension is pure observability, added because the
# per-PURPOSE ledger above cannot answer "why is Gemini exhausted": Gemini spend is
# split across the swing/intraday/options purpose buckets plus an entirely unmetered
# health-probe path, so no single bucket ever shows it as the cause. Gemini's free
# tier is also per-REQUEST, not per-token, which the purpose ledger has no way to
# represent at all -- hence tracking requests here too, not just tokens.
_TRACKED_PROVIDERS = ("anthropic", "gemini", "groq", "cloudflare", "ollama")


class LLMBudgetExhausted(RuntimeError):
    """A purpose has used its reserved daily token share."""


def budget_config() -> dict[str, Any]:
    """`llm.token_budgets` block of global.json ({} when absent/unreadable)."""
    try:
        block = (
            json.loads(_CFG_PATH.read_text(encoding="utf-8"))
            .get("llm", {})
            .get("token_budgets", {})
        )
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def daily_limit(purpose: str) -> int | None:
    """Reserved tokens/day for `purpose`. None = unmetered (budgets disabled or the purpose
    is not listed); 0 = the purpose may not call the LLM at all."""
    cfg = budget_config()
    if not cfg.get("enabled", False):
        return None
    limits = cfg.get("daily_tokens", {})
    if not isinstance(limits, dict) or limits.get(purpose) is None:
        return None
    try:
        return max(0, int(limits[purpose]))
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(_IST).date().isoformat()


class TokenLedger:
    def __init__(self) -> None:
        self._redis: Any = None
        self._mem: dict[str, int] = {}
        self._warned: set[str] = set()
        self._mem_seen: dict[str, float] = {}
        # digest -> (monotonic time stored, json payload): duplicate-prompt answer cache.
        self._mem_resp: dict[str, tuple[float, str]] = {}

    def configure(self, redis_client: Any) -> None:
        """Persist counts in this Redis client (call once at boot). Optional."""
        self._redis = redis_client

    def _key(self, purpose: str) -> str:
        return f"{_KEY_PREFIX}:{purpose}:{_today()}"

    async def spent(self, purpose: str) -> int:
        key = self._key(purpose)
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return int(raw) if raw else 0
            except Exception:
                pass
        return self._mem.get(key, 0)

    async def remaining(self, purpose: str) -> int | None:
        """Tokens left today, or None when the purpose is unmetered."""
        limit = daily_limit(purpose)
        if limit is None:
            return None
        return max(0, limit - await self.spent(purpose))

    async def add(self, purpose: str, tokens: int) -> None:
        if tokens <= 0 or daily_limit(purpose) is None:
            return
        key = self._key(purpose)
        total = await self.spent(purpose) + tokens
        self._mem[key] = total
        if self._redis is not None:
            try:
                await self._redis.setex(key, 172_800, str(total))
            except Exception:
                pass

    def note_exhausted(self, purpose: str) -> None:
        """Log once per purpose per day so an exhausted budget is visible, not silent."""
        key = self._key(purpose)
        if key not in self._warned:
            self._warned.add(key)
            _log.warning(
                "llm_budget_exhausted purpose=%s limit=%s — calls refused until tomorrow "
                "or the limit is raised in config/global.json llm.token_budgets",
                purpose,
                daily_limit(purpose),
            )

    async def seen_recently(self, key: str, ttl_seconds: int) -> bool:
        """Atomically record `key` and report whether it was ALREADY recorded within the last
        `ttl_seconds` (Redis-backed so it survives an orchestrator restart -- the restart loop
        re-sent identical prompts across processes; in-memory fallback when Redis is down)."""
        import time as _time

        if self._redis is not None:
            try:
                created = await self._redis.set(
                    f"llm:seen:{key}", "1", nx=True, ex=int(ttl_seconds)
                )
                return not created
            except Exception:  # noqa: BLE001
                pass
        now = _time.monotonic()
        last = self._mem_seen.get(key)
        self._mem_seen[key] = now
        return last is not None and now - last < ttl_seconds

    async def cached_response(self, key: str, ttl_seconds: int) -> tuple[float, str] | None:
        """(age_seconds, payload) of a stored answer for `key` younger than ttl, else None.
        Redis-backed (survives restarts), in-memory fallback."""
        import json as _json
        import time as _time

        if self._redis is not None:
            try:
                raw = await self._redis.get(f"llm:resp:{key}")
                if raw:
                    d = _json.loads(raw)
                    age = _time.time() - float(d["ts"])
                    return (age, str(d["payload"])) if age < ttl_seconds else None
                return None
            except Exception:  # noqa: BLE001
                pass
        hit = self._mem_resp.get(key)
        if hit is None:
            return None
        age = _time.monotonic() - hit[0]
        return (age, hit[1]) if age < ttl_seconds else None

    async def store_response(self, key: str, payload: str, ttl_seconds: int) -> None:
        import json as _json
        import time as _time

        self._mem_resp[key] = (_time.monotonic(), payload)
        if self._redis is not None:
            try:
                await self._redis.set(
                    f"llm:resp:{key}",
                    _json.dumps({"ts": _time.time(), "payload": payload}),
                    ex=int(ttl_seconds),
                )
            except Exception:  # noqa: BLE001
                pass

    # ── Per-provider tracking (D1/D2, 2026-09-23) — unbudgeted, observability only ──
    # Recorded from src.llm.providers.LLMClient._dispatch, once per provider ATTEMPT
    # (not once per successful complete() call): a killed or failed attempt still
    # spends real API-side work and used to be recorded as zero. _dispatch charges an
    # estimate before the call (charge on send) and corrects it with real usage after
    # a successful return where the provider reports it (reconcile on return) --
    # groq/cloudflare do; anthropic/gemini/ollama do not, so their figures stay
    # estimates. Deliberately not gated by daily_limit/enabled: this exists to show
    # spend, not to enforce it -- enforcement stays entirely in the purpose-based
    # methods above.

    def _provider_tokens_key(self, provider: str) -> str:
        return f"{_PROVIDER_TOKENS_PREFIX}:{provider}:{_today()}"

    def _provider_requests_key(self, provider: str) -> str:
        return f"{_PROVIDER_REQUESTS_PREFIX}:{provider}:{_today()}"

    async def _get_int(self, key: str) -> int:
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return int(raw) if raw else 0
            except Exception:
                pass
        return self._mem.get(key, 0)

    async def _incr_int(self, key: str, delta: int) -> None:
        if delta == 0:
            return
        total = max(0, await self._get_int(key) + delta)
        self._mem[key] = total
        if self._redis is not None:
            try:
                await self._redis.setex(key, 172_800, str(total))
            except Exception:
                pass

    async def add_provider_tokens(self, provider: str, tokens: int) -> None:
        """`tokens` may be negative (a reconciling correction) — the running total is
        floored at 0 either way."""
        await self._incr_int(self._provider_tokens_key(provider), tokens)

    async def add_provider_request(self, provider: str) -> None:
        await self._incr_int(self._provider_requests_key(provider), 1)

    async def provider_tokens_spent(self, provider: str) -> int:
        return await self._get_int(self._provider_tokens_key(provider))

    async def provider_requests_made(self, provider: str) -> int:
        return await self._get_int(self._provider_requests_key(provider))

    async def provider_snapshot(self) -> dict[str, dict[str, int]]:
        """{provider: {"tokens": N, "requests": N}} for every tracked provider,
        today. Used by the D5 dashboard surface (GET /api/llm_tokens)."""
        out: dict[str, dict[str, int]] = {}
        for prov in _TRACKED_PROVIDERS:
            out[prov] = {
                "tokens": await self.provider_tokens_spent(prov),
                "requests": await self.provider_requests_made(prov),
            }
        return out

    async def purpose_snapshot(self) -> dict[str, dict[str, int | None]]:
        """{purpose: {"spent": N, "limit": N|None}} for every configured purpose,
        today. Used alongside provider_snapshot() by the D5 dashboard surface."""
        cfg = budget_config()
        purposes = cfg.get("daily_tokens", {})
        out: dict[str, dict[str, int | None]] = {}
        if isinstance(purposes, dict):
            for purpose in purposes:
                out[str(purpose)] = {
                    "spent": await self.spent(str(purpose)),
                    "limit": daily_limit(str(purpose)),
                }
        return out


LEDGER = TokenLedger()


def estimate_tokens(*texts: str) -> int:
    """~4 chars/token — only used when a provider reports no usage figure."""
    return max(1, sum(len(t or "") for t in texts) // 4)
