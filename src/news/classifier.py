"""
News Classifier â€” Gemini Flash classification of NewsItem â†’ NewsClassification.

The LLM client is injected so this module is testable without live API calls.
In production, inject a GeminiClient; in tests, inject a stub.

NewsClassification schema per spec (Â§7):
  symbol, scope, event_type, sentiment, magnitude, confidence,
  trading_action, avoid_duration_minutes, reasoning, affected_symbols
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from src.infrastructure.logger import get_logger
from src.news.poller import NewsItem

if TYPE_CHECKING:
    from src.news.token_budget import DailyTokenBudget

logger = get_logger(__name__)

_VALID_SCOPES = {"COMPANY", "SECTOR", "MACRO"}
_VALID_EVENT_TYPES = {
    "EARNINGS",
    "MERGER",
    "REGULATORY",
    "MANAGEMENT",
    "MACRO",
    "SENTIMENT",
    "OTHER",
}
_VALID_SENTIMENTS = {"BULLISH", "BEARISH", "NEUTRAL"}
_VALID_MAGNITUDES = {"HIGH", "MEDIUM", "LOW"}
_VALID_ACTIONS = {"HALT", "AVOID", "BOOST", "DECAY", "MONITOR"}

_CLASSIFICATION_PROMPT = """You are a financial news classifier for an Indian equity trading system.
Classify the following news. Return ONLY valid JSON with these exact fields:
{{
  "symbol": "<stock symbol or NIFTY50 or SECTOR:BANKING etc>",
  "scope": "<COMPANY|SECTOR|MACRO>",
  "event_type": "<EARNINGS|MERGER|REGULATORY|MANAGEMENT|MACRO|SENTIMENT|OTHER>",
  "sentiment": "<BULLISH|BEARISH|NEUTRAL>",
  "magnitude": "<HIGH|MEDIUM|LOW>",
  "confidence": <0.0-1.0>,
  "trading_action": "<HALT|AVOID|BOOST|DECAY|MONITOR>",
  "avoid_duration_minutes": <integer>,
  "reasoning": "<one sentence>",
  "affected_symbols": [<list of strings>]
}}
Headline: {headline}
Content: {content}
Known symbols: {raw_symbols}"""


@dataclass
class NewsClassification:
    symbol: str
    scope: str
    event_type: str
    sentiment: str
    magnitude: str
    confidence: float
    trading_action: str
    avoid_duration_minutes: int
    reasoning: str
    affected_symbols: list[str] = field(default_factory=list)


class LLMClient(Protocol):
    async def complete(self, prompt: str) -> str: ...


class NewsClassifier:
    """Classifies a NewsItem via an injected LLM client."""

    def __init__(
        self,
        llm: LLMClient,
        timeout_seconds: int = 5,
        fallback_to_keyword: bool = True,
        token_budget: DailyTokenBudget | None = None,
        budget_provider: str = "groq",
    ) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds
        self._fallback_to_keyword = fallback_to_keyword
        # Optional daily cap on tokens attributed to `budget_provider` (the
        # provider a shared quota needs protecting from — see module docstring
        # and src/news/token_budget.py). None (default) = unlimited, i.e. the
        # exact pre-existing behaviour for any caller that doesn't wire one in.
        self._token_budget = token_budget
        self._budget_provider = budget_provider

    async def classify(self, item: NewsItem) -> NewsClassification:
        # NSE filings with no summary text are just a generic category label
        # ("Reply to Clarification..."): the LLM has nothing to add, and each call
        # spends the capped daily quota that trade-critical G9 reviews share.
        if item.source == "NSE" and item.content.strip() == item.headline.strip():
            return _keyword_classify(item)
        prompt = _CLASSIFICATION_PROMPT.format(
            headline=item.headline,
            content=item.content[:500],
            raw_symbols=", ".join(item.raw_symbols) or "unknown",
        )
        if self._token_budget is not None:
            today = datetime.now(UTC).date().isoformat()
            if await self._token_budget.remaining(today) <= 0:
                # Skip the LLM call entirely — no point spending an API round
                # trip just to be refused, and this is the whole point: a
                # non-trade-critical caller must stop competing for the quota,
                # not merely stop paying for it. Same fallback as any other
                # classify() failure (news already tolerates this).
                logger.debug("news_classify_budget_exhausted headline=%r", item.headline[:60])
                return (
                    _keyword_classify(item)
                    if self._fallback_to_keyword
                    else (_fallback_classification(item))
                )
        try:
            raw = await self._llm.complete(prompt)
            if self._token_budget is not None:
                used_provider = getattr(self._llm, "last_provider", None)
                if used_provider is not None and str(used_provider) == self._budget_provider:
                    tokens = getattr(self._llm, "last_tokens", None)
                    if tokens:
                        await self._token_budget.add(today, tokens)
            return _parse_classification(raw, item)
        except Exception:
            if self._fallback_to_keyword:
                return _keyword_classify(item)
            logger.warning("classify_failed headline=%r", item.headline[:60])
            return _fallback_classification(item)


def _parse_classification(raw: str, item: NewsItem) -> NewsClassification:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    data = json.loads(text)

    def _val(key: str, valid: set[str], default: str) -> str:
        v = data.get(key, default)
        return str(v) if v in valid else default

    confidence = float(data.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))

    return NewsClassification(
        symbol=str(data.get("symbol", item.raw_symbols[0] if item.raw_symbols else "UNKNOWN")),
        scope=_val("scope", _VALID_SCOPES, "MACRO"),
        event_type=_val("event_type", _VALID_EVENT_TYPES, "OTHER"),
        sentiment=_val("sentiment", _VALID_SENTIMENTS, "NEUTRAL"),
        magnitude=_val("magnitude", _VALID_MAGNITUDES, "LOW"),
        confidence=confidence,
        trading_action=_val("trading_action", _VALID_ACTIONS, "MONITOR"),
        avoid_duration_minutes=int(data.get("avoid_duration_minutes", 0)),
        reasoning=str(data.get("reasoning", ""))[:200],
        affected_symbols=list(data.get("affected_symbols", [])),
    )


def _keyword_classify(item: NewsItem) -> NewsClassification:
    """Keyword-based classification when LLM is unavailable."""
    text = (item.headline + " " + item.content[:200]).lower()
    symbol = item.raw_symbols[0] if item.raw_symbols else "UNKNOWN"

    _HALT = ["sebi investigation", "fraud", "ban", "suspended", "rbi ban", "probe sebi"]
    _AVOID = [
        "npa",
        "downgrade",
        "misses",
        "probe",
        "loss widens",
        "profit falls",
        "miss estimates",
    ]
    _BOOST = [
        "beats estimates",
        "record profit",
        "upgrade",
        "buyback",
        "strong results",
        "dividend",
    ]
    _BEARISH = ["decline", "fall", "loss", "negative", "weak", "miss", "cut", "drop"]
    _BULLISH = [
        "profit",
        "growth",
        "strong",
        "record",
        "beat",
        "upgrade",
        "dividend",
        "acquire",
        "acquisition",
        "surge",
        "52-week high",
        "order win",
        "bagging",
    ]

    action, sentiment, magnitude, reason = "MONITOR", "NEUTRAL", "LOW", ""

    for kw in _HALT:
        if kw in text:
            action, sentiment, magnitude, reason = "HALT", "BEARISH", "HIGH", f"halt signal: {kw}"
            break
    if not reason:
        for kw in _AVOID:
            if kw in text:
                action, sentiment, magnitude, reason = (
                    "AVOID",
                    "BEARISH",
                    "MEDIUM",
                    f"avoid signal: {kw}",
                )
                break
    if not reason:
        for kw in _BOOST:
            if kw in text:
                action, sentiment, magnitude, reason = (
                    "BOOST",
                    "BULLISH",
                    "MEDIUM",
                    f"boost signal: {kw}",
                )
                break
    if not reason:
        bull = sum(1 for k in _BULLISH if k in text)
        bear = sum(1 for k in _BEARISH if k in text)
        if bull > bear:
            sentiment, reason = "BULLISH", "positive indicators in headline"
        elif bear > bull:
            sentiment, reason = "BEARISH", "negative indicators in headline"
        else:
            reason = "no significant signals detected"

    return NewsClassification(
        symbol=symbol,
        scope="COMPANY" if item.raw_symbols else "MACRO",
        event_type="OTHER",
        sentiment=sentiment,
        magnitude=magnitude,
        confidence=0.4,
        trading_action=action,
        avoid_duration_minutes=120 if action == "AVOID" else 0,
        reasoning=reason,
        affected_symbols=list(item.raw_symbols),
    )


def _fallback_classification(item: NewsItem) -> NewsClassification:
    symbol = item.raw_symbols[0] if item.raw_symbols else "UNKNOWN"
    return NewsClassification(
        symbol=symbol,
        scope="COMPANY" if item.raw_symbols else "MACRO",
        event_type="OTHER",
        sentiment="NEUTRAL",
        magnitude="LOW",
        confidence=0.3,
        trading_action="MONITOR",
        avoid_duration_minutes=0,
        reasoning="LLM classification failed â€” fallback to MONITOR",
        affected_symbols=list(item.raw_symbols),
    )
