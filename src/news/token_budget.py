"""Daily token-spend budget for one LLM-consuming path, backed by Redis so it
survives a restart.

Built 2026-09-15: the orchestrator sat idle ~3.5 days (Fri 17:34 -> Tue 09:27),
long enough to outlive the news dedup TTL (24h, src/news/deduplicator.py). The
first poll after restart saw 411 previously-classified items as "fresh" again,
and 658 classify() calls in the next 90 minutes -- all routed through the
Groq-first `background` LLM chain -- exhausted Groq's whole 200k/day cap.
That starved the llm_intraday_agent / llm_swing_agent G9 reviews
(also Groq-first, deliberately Gemini-free -- see llm.intraday_g9/swing_g9 in
config/global.json) of their only reviewer for the rest of the day, silently
failing every one of those strategies' trades closed.

News classification already has a designed, tested fallback for any LLM
failure (NewsClassifier's keyword classifier) -- this budget just gives
"we'd blow the shared quota" the same treatment as any other LLM failure,
BEFORE spending the call, not after.
"""

from __future__ import annotations


class DailyTokenBudget:
    """Tracks cumulative token spend per calendar day under one Redis key
    prefix. Not exact accounting (concurrent callers can race a read-modify-
    write), which is fine here: this guards against a runaway multi-hundred-
    call burst, not a handful of tokens of drift.
    """

    def __init__(self, redis_client: object, key_prefix: str, daily_limit: int) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._limit = max(0, daily_limit)

    def _key(self, day: str) -> str:
        return f"{self._prefix}:{day}"

    async def remaining(self, day: str) -> int:
        """Tokens still available today (>= 0, never negative)."""
        raw = await self._redis.get(self._key(day))  # type: ignore[attr-defined]
        spent = int(raw) if raw else 0
        return max(0, self._limit - spent)

    async def add(self, day: str, tokens: int) -> None:
        """Record `tokens` spent today. A 2-day TTL means an idle process
        doesn't need its own cleanup job to stop the key growing forever."""
        if tokens <= 0:
            return
        raw = await self._redis.get(self._key(day))  # type: ignore[attr-defined]
        spent = int(raw) if raw else 0
        await self._redis.setex(self._key(day), 172_800, str(spent + tokens))  # type: ignore[attr-defined]
