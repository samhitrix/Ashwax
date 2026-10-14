"""
News Impact Router â€” maps NewsClassification â†’ HALT/AVOID/BOOST/DECAY/MONITOR actions.

Per spec Â§7:
  HALT    â€” circuit breaker; flag open positions for emergency review
  AVOID   â€” Redis symbol_news_status:{symbol} = AVOID with TTL = avoid_duration_minutes
  BOOST   â€” +0.15 confidence delta for pending signals (capped at 1.0)
  DECAY   â€” -0.20 confidence delta for counter-directional signals
  MONITOR â€” log only

Redis key: symbol_news_status:{SYMBOL}
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.infrastructure.logger import get_logger
from src.news.classifier import NewsClassification

logger = get_logger(__name__)

_REDIS_KEY_PREFIX = "symbol_news_status:"
_CONFIDENCE_BOOST = 0.15
_CONFIDENCE_DECAY = 0.20
_DEFAULT_TTL_SECONDS = 3600


class TradingAction(StrEnum):
    HALT = "HALT"
    AVOID = "AVOID"
    BOOST = "BOOST"
    DECAY = "DECAY"
    MONITOR = "MONITOR"


@dataclass
class RouteResult:
    action: TradingAction
    symbol: str
    confidence_delta: float
    redis_key: str
    redis_ttl_seconds: int
    requires_position_review: bool


class RedisWriter(Protocol):
    async def set(self, key: str, value: str, ex: int) -> None: ...


class ImpactRouter:
    """Routes a NewsClassification to a RouteResult and writes Redis state."""

    def __init__(
        self,
        redis: RedisWriter,
        avoid_duration_minutes: int = 120,
    ) -> None:
        self._redis = redis
        self._avoid_duration_minutes = avoid_duration_minutes

    async def route(self, nc: NewsClassification) -> RouteResult:
        action = TradingAction(nc.trading_action)
        result = _build_result(action, nc)

        if action in (TradingAction.HALT, TradingAction.AVOID):
            if nc.symbol == "UNKNOWN":
                # Macro-scope news with no specific ticker attached (empty
                # raw_symbols) — a Redis key literally named "UNKNOWN" can never
                # match a real trade's symbol, so writing it and warning on it
                # was pure noise (13x/day, 2026-08-12). The real per-symbol
                # circuit breaker is orchestrator.py's shared["news_avoid_symbols"],
                # populated separately from classification.affected_symbols,
                # which is empty here for the same reason symbol is unknown.
                logger.debug(
                    "news_impact_macro action=%s (no symbol) ttl=%ds",
                    action,
                    result.redis_ttl_seconds,
                )
            else:
                await self._redis.set(result.redis_key, action.value, ex=result.redis_ttl_seconds)
                logger.warning(
                    "news_impact action=%s symbol=%s ttl=%ds",
                    action,
                    nc.symbol,
                    result.redis_ttl_seconds,
                )
        elif action == TradingAction.BOOST:
            logger.info("news_boost symbol=%s delta=+%.2f", nc.symbol, _CONFIDENCE_BOOST)
        elif action == TradingAction.DECAY:
            logger.info("news_decay symbol=%s delta=%.2f", nc.symbol, -_CONFIDENCE_DECAY)
        else:
            logger.debug("news_monitor symbol=%s", nc.symbol)

        return result


def _build_result(action: TradingAction, nc: NewsClassification) -> RouteResult:
    ttl = nc.avoid_duration_minutes * 60 if nc.avoid_duration_minutes > 0 else _DEFAULT_TTL_SECONDS
    redis_key = (
        _REDIS_KEY_PREFIX + nc.symbol if action in (TradingAction.HALT, TradingAction.AVOID) else ""
    )

    confidence_delta = 0.0
    if action == TradingAction.BOOST:
        confidence_delta = _CONFIDENCE_BOOST
    elif action == TradingAction.DECAY:
        confidence_delta = -_CONFIDENCE_DECAY

    return RouteResult(
        action=action,
        symbol=nc.symbol,
        confidence_delta=confidence_delta,
        redis_key=redis_key,
        redis_ttl_seconds=ttl,
        requires_position_review=(action == TradingAction.HALT),
    )


def apply_confidence_delta(base_confidence: float, delta: float) -> float:
    """Clamp to [0.0, 1.0]."""
    return max(0.0, min(1.0, base_confidence + delta))
