"""Unit tests for src/news/ — all external dependencies stubbed in-process."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from src.news.classifier import NewsClassification, NewsClassifier
from src.news.deduplicator import Deduplicator
from src.news.impact_router import ImpactRouter, TradingAction, apply_confidence_delta
from src.news.opportunity_scanner import OpportunityScanner
from src.news.poller import NewsItem, NewsPoller, _parse_rss
from src.news.state_store import NewsStateStore
from src.news.token_budget import DailyTokenBudget

_IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def exists(self, key: str) -> bool:
        return key in self._store

    async def set(self, key: str, value: str, ex: int) -> None:
        self._store[key] = value


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str) -> str:
        return self._response


class _FakeHttp:
    def __init__(self, text: str) -> None:
        self._text = text

    async def get_text(self, url: str) -> str:
        return self._text


class _FakeRepo:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def insert(self, record: dict[str, object]) -> None:
        self.records.append(record)


def _make_item(headline: str = "Test news", symbols: list[str] | None = None) -> NewsItem:
    return NewsItem(
        source="TEST",
        headline=headline,
        url="https://example.com/news",
        raw_symbols=symbols or ["RELIANCE"],
        published_at=datetime.now(_IST),
        content=headline,
    )


def _make_nc(
    symbol: str = "RELIANCE",
    trading_action: str = "MONITOR",
    magnitude: str = "LOW",
    sentiment: str = "NEUTRAL",
    confidence: float = 0.5,
    avoid_duration_minutes: int = 0,
) -> NewsClassification:
    return NewsClassification(
        symbol=symbol,
        scope="COMPANY",
        event_type="OTHER",
        sentiment=sentiment,
        magnitude=magnitude,
        confidence=confidence,
        trading_action=trading_action,
        avoid_duration_minutes=avoid_duration_minutes,
        reasoning="test",
        affected_symbols=[],
    )


# ---------------------------------------------------------------------------
# poller
# ---------------------------------------------------------------------------


def test_newsitem_dedup_hash_deterministic():
    item1 = _make_item("RELIANCE Q3 profits up 20%")
    item2 = _make_item("RELIANCE Q3 profits up 20%")
    assert item1.dedup_hash == item2.dedup_hash


def test_newsitem_different_headlines_different_hash():
    item1 = _make_item("RELIANCE Q3 profits up 20%")
    item2 = _make_item("TCS acquires startup")
    assert item1.dedup_hash != item2.dedup_hash


def test_parse_rss_valid():
    xml = (
        '<?xml version="1.0"?>'
        '<rss version="2.0"><channel>'
        "<item><title>Markets rally on RBI rate cut</title><link>https://example.com/1</link></item>"
        "<item><title>NIFTY hits all-time high</title><link>https://example.com/2</link></item>"
        "</channel></rss>"
    )
    items = _parse_rss("ET", xml)
    assert len(items) == 2
    assert items[0].headline == "Markets rally on RBI rate cut"
    assert items[0].source == "ET"


def test_parse_rss_invalid_xml_returns_empty():
    items = _parse_rss("ET", "not xml at all <><>")
    assert items == []


async def test_poller_poll_nse():
    poller = NewsPoller(_FakeHttp(""))
    payload = json.dumps([{"desc": "Board meeting", "symbol": "INFY", "attchmntFile": "f.pdf"}])
    items = await poller.poll_nse(payload)
    assert len(items) == 1
    assert items[0].source == "NSE"
    assert "INFY" in items[0].raw_symbols


async def test_poller_poll_nse_keeps_announcement_summary_for_classifier():
    poller = NewsPoller(_FakeHttp(""))
    payload = json.dumps(
        [
            {
                "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
                "symbol": "MARUTI",
                "attchmntText": "Company announces acquisition of a 20% stake",
                "attchmntFile": "m.pdf",
            }
        ]
    )
    items = await poller.poll_nse(payload)
    assert items[0].headline == "Analysts/Institutional Investor Meet/Con. Call Updates"
    assert "acquisition" in items[0].content


async def test_poller_poll_bse():
    poller = NewsPoller(_FakeHttp(""))
    payload = json.dumps(
        [{"NEWSSUB": "Quarterly results", "SCRIP_CD": "500325", "ATTACHMENTNAME": "q3.pdf"}]
    )
    items = await poller.poll_bse(payload)
    assert len(items) == 1
    assert items[0].source == "BSE"


async def test_poller_poll_rss_uses_http():
    xml = (
        '<?xml version="1.0"?><rss version="2.0"><channel>'
        "<item><title>RBI policy</title><link>http://x.com</link></item>"
        "</channel></rss>"
    )
    poller = NewsPoller(_FakeHttp(xml))
    items = await poller.poll_rss("ET", "http://any-url.com")
    assert len(items) == 1


async def test_poller_poll_rss_http_error_returns_empty():
    class _BrokenHttp:
        async def get_text(self, url: str) -> str:
            raise ConnectionError("timeout")

    poller = NewsPoller(_BrokenHttp())
    items = await poller.poll_rss("ET", "http://broken.url")
    assert items == []


# ---------------------------------------------------------------------------
# deduplicator
# ---------------------------------------------------------------------------


async def test_dedup_new_item_not_seen():
    dedup = Deduplicator(_FakeRedis())
    assert not await dedup.is_seen("abc123")


async def test_dedup_mark_then_seen():
    dedup = Deduplicator(_FakeRedis())
    await dedup.mark_seen("abc123")
    assert await dedup.is_seen("abc123")


async def test_dedup_different_hashes_independent():
    dedup = Deduplicator(_FakeRedis())
    await dedup.mark_seen("hash_a")
    assert not await dedup.is_seen("hash_b")


# ---------------------------------------------------------------------------
# classifier
# ---------------------------------------------------------------------------


async def test_classifier_valid_json_response():
    response = json.dumps(
        {
            "symbol": "RELIANCE",
            "scope": "COMPANY",
            "event_type": "EARNINGS",
            "sentiment": "BULLISH",
            "magnitude": "HIGH",
            "confidence": 0.85,
            "trading_action": "BOOST",
            "avoid_duration_minutes": 0,
            "reasoning": "Strong quarterly results",
            "affected_symbols": ["RELIANCE"],
        }
    )
    classifier = NewsClassifier(_FakeLLM(response))
    nc = await classifier.classify(_make_item())
    assert nc.symbol == "RELIANCE"
    assert nc.trading_action == "BOOST"
    assert nc.magnitude == "HIGH"
    assert nc.confidence == pytest.approx(0.85)


async def test_classifier_llm_failure_returns_fallback():
    class _BrokenLLM:
        async def complete(self, prompt: str) -> str:
            raise RuntimeError("API down")

    classifier = NewsClassifier(_BrokenLLM())
    nc = await classifier.classify(_make_item())
    # keyword fallback (fallback_to_keyword=True default) returns confidence=0.4
    assert nc.confidence == pytest.approx(0.4)
    assert nc.trading_action in {"MONITOR", "AVOID", "HALT", "BOOST"}


async def test_classifier_nse_without_summary_skips_llm():
    calls: list[str] = []

    class _CountingLLM:
        async def complete(self, prompt: str) -> str:
            calls.append(prompt)
            raise AssertionError("LLM must not be called")

    label = "Reply to Clarification- Financial results"
    generic = NewsItem(
        source="NSE",
        headline=label,
        url="u",
        raw_symbols=["RMCL"],
        published_at=datetime.now(_IST),
        content=label,
    )
    nc = await NewsClassifier(_CountingLLM()).classify(generic)
    assert calls == []
    assert nc.confidence == pytest.approx(0.4)


async def test_keyword_classifier_recognises_acquisition():
    item = NewsItem(
        source="NSE",
        headline="Board meeting",
        url="u",
        raw_symbols=["MARUTI"],
        published_at=datetime.now(_IST),
        content="Board meeting. Announces acquisition of stake",
    )
    nc = await NewsClassifier(_FakeLLM("not json")).classify(item)
    assert nc.sentiment == "BULLISH"


async def test_classifier_invalid_enum_defaults():
    response = json.dumps(
        {
            "symbol": "TCS",
            "scope": "INVALID_SCOPE",
            "event_type": "EARNINGS",
            "sentiment": "VERY_BULLISH",
            "magnitude": "SUPER_HIGH",
            "confidence": 0.7,
            "trading_action": "MAGIC",
            "avoid_duration_minutes": 0,
            "reasoning": "test",
            "affected_symbols": [],
        }
    )
    classifier = NewsClassifier(_FakeLLM(response))
    nc = await classifier.classify(_make_item("TCS wins deal", ["TCS"]))
    assert nc.scope == "MACRO"
    assert nc.sentiment == "NEUTRAL"
    assert nc.trading_action == "MONITOR"


async def test_classifier_markdown_fenced_json():
    inner = json.dumps(
        {
            "symbol": "HDFC",
            "scope": "COMPANY",
            "event_type": "MANAGEMENT",
            "sentiment": "NEUTRAL",
            "magnitude": "LOW",
            "confidence": 0.6,
            "trading_action": "MONITOR",
            "avoid_duration_minutes": 0,
            "reasoning": "CEO change",
            "affected_symbols": [],
        }
    )
    response = f"```json\n{inner}\n```"
    classifier = NewsClassifier(_FakeLLM(response))
    nc = await classifier.classify(_make_item("HDFC CEO resigns", ["HDFC"]))
    assert nc.event_type == "MANAGEMENT"


# ---------------------------------------------------------------------------
# classifier — daily token budget (2026-09-15: 658 Groq calls in 90 minutes
# after a 3.5-day idle gap outlived the dedup TTL burned Groq's whole 200k/day
# cap, starving the Groq-first G9 trade reviews of their only reviewer)
# ---------------------------------------------------------------------------

_VALID_RESPONSE = json.dumps(
    {
        "symbol": "RELIANCE",
        "scope": "COMPANY",
        "event_type": "EARNINGS",
        "sentiment": "BULLISH",
        "magnitude": "HIGH",
        "confidence": 0.85,
        "trading_action": "BOOST",
        "avoid_duration_minutes": 0,
        "reasoning": "test",
        "affected_symbols": [],
    }
)


class _BudgetRedis:
    """The get/setex subset DailyTokenBudget needs."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._data[key] = value


class _FakeLLMWithUsage:
    """Reports which provider served the call and how many tokens it billed —
    the two side-channel attributes NewsClassifier reads to attribute spend."""

    def __init__(self, response: str, provider: str = "groq", tokens: int | None = 500) -> None:
        self._response = response
        self.last_provider = provider
        self.last_tokens = tokens
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._response


async def test_budget_available_calls_llm_and_records_groq_spend() -> None:
    budget = DailyTokenBudget(_BudgetRedis(), "news:llm_tokens", daily_limit=10_000)
    llm = _FakeLLMWithUsage(_VALID_RESPONSE, provider="groq", tokens=500)
    classifier = NewsClassifier(llm, token_budget=budget)

    nc = await classifier.classify(_make_item())

    assert nc.symbol == "RELIANCE"  # the LLM path ran, not the keyword fallback
    assert llm.calls == 1
    today = datetime.now(UTC).date().isoformat()
    assert await budget.remaining(today) == 9_500


async def test_budget_exhausted_skips_llm_entirely() -> None:
    budget = DailyTokenBudget(_BudgetRedis(), "news:llm_tokens", daily_limit=100)
    llm = _FakeLLMWithUsage(_VALID_RESPONSE, provider="groq", tokens=500)
    today = datetime.now(UTC).date().isoformat()
    await budget.add(today, 100)  # already at the cap
    classifier = NewsClassifier(llm, token_budget=budget)

    nc = await classifier.classify(_make_item())

    assert llm.calls == 0  # no API round trip spent once exhausted
    assert nc.confidence == pytest.approx(0.4)  # keyword fallback, same as an LLM failure


async def test_budget_ignores_spend_from_a_different_provider() -> None:
    """If Groq's chain falls through to Ollama, that spend must NOT count
    against Groq's own budget — Ollama isn't the quota being protected."""
    budget = DailyTokenBudget(_BudgetRedis(), "news:llm_tokens", daily_limit=10_000)
    llm = _FakeLLMWithUsage(_VALID_RESPONSE, provider="ollama", tokens=500)
    classifier = NewsClassifier(llm, token_budget=budget, budget_provider="groq")

    await classifier.classify(_make_item())

    today = datetime.now(UTC).date().isoformat()
    assert await budget.remaining(today) == 10_000  # untouched


async def test_no_budget_configured_preserves_prior_unlimited_behaviour() -> None:
    """Default token_budget=None must behave exactly as before this feature."""
    llm = _FakeLLM(_VALID_RESPONSE)
    classifier = NewsClassifier(llm)  # no token_budget passed
    nc = await classifier.classify(_make_item())
    assert nc.symbol == "RELIANCE"


# ---------------------------------------------------------------------------
# impact_router
# ---------------------------------------------------------------------------


async def test_router_avoid_writes_redis():
    redis = _FakeRedis()
    router = ImpactRouter(redis)
    nc = _make_nc("RELIANCE", "AVOID", "HIGH", "BEARISH", 0.8, 60)
    result = await router.route(nc)
    assert result.action == TradingAction.AVOID
    assert await redis.exists("symbol_news_status:RELIANCE")
    assert result.redis_ttl_seconds == 3600


async def test_router_halt_unknown_symbol_skips_redis_write():
    # Regression (2026-08-12): macro-scope news with no ticker attached
    # (symbol="UNKNOWN") was writing a Redis key literally named
    # "symbol_news_status:UNKNOWN" and warning on it 13x/day — a key no real
    # trade's symbol could ever match. Should be a no-op write (still logs,
    # just not at WARNING and not to Redis).
    redis = _FakeRedis()
    router = ImpactRouter(redis)
    nc = _make_nc("UNKNOWN", "HALT", "HIGH", "BEARISH", 0.9, 60)
    result = await router.route(nc)
    assert result.action == TradingAction.HALT
    assert not await redis.exists("symbol_news_status:UNKNOWN")


async def test_router_halt_requires_position_review():
    redis = _FakeRedis()
    router = ImpactRouter(redis)
    nc = _make_nc("ADANI", "HALT", "HIGH", "BEARISH", 0.9, 120)
    result = await router.route(nc)
    assert result.requires_position_review is True


async def test_router_boost_no_redis_write():
    redis = _FakeRedis()
    router = ImpactRouter(redis)
    nc = _make_nc("TCS", "BOOST", "HIGH", "BULLISH", 0.85, 0)
    result = await router.route(nc)
    assert result.confidence_delta == pytest.approx(0.15)
    assert result.redis_key == ""


async def test_router_decay_confidence_delta():
    redis = _FakeRedis()
    router = ImpactRouter(redis)
    nc = _make_nc("INFY", "DECAY", "MEDIUM", "BEARISH", 0.65, 0)
    result = await router.route(nc)
    assert result.confidence_delta == pytest.approx(-0.20)


def test_apply_confidence_delta_clamped():
    assert apply_confidence_delta(0.9, 0.15) == pytest.approx(1.0)
    assert apply_confidence_delta(0.1, -0.20) == pytest.approx(0.0)
    assert apply_confidence_delta(0.7, 0.15) == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# opportunity_scanner
# ---------------------------------------------------------------------------


def test_scanner_boost_high_bullish_generates_opportunity():
    scanner = OpportunityScanner()
    nc2 = NewsClassification(
        symbol="RELIANCE",
        scope="COMPANY",
        event_type="EARNINGS",
        sentiment="BULLISH",
        magnitude="HIGH",
        confidence=0.85,
        trading_action="BOOST",
        avoid_duration_minutes=0,
        reasoning="Record profits",
        affected_symbols=["RELIANCE", "IOC"],
    )
    opps = scanner.scan(nc2)
    assert len(opps) == 2
    assert opps[0].symbol == "RELIANCE"
    assert opps[0].direction == "LONG"


def test_scanner_avoid_high_bearish_generates_short_opportunity():
    scanner = OpportunityScanner()
    nc = NewsClassification(
        symbol="ZOMATO",
        scope="COMPANY",
        event_type="REGULATORY",
        sentiment="BEARISH",
        magnitude="HIGH",
        confidence=0.80,
        trading_action="AVOID",
        avoid_duration_minutes=120,
        reasoning="SEBI probe",
        affected_symbols=["ZOMATO"],
    )
    opps = scanner.scan(nc)
    assert len(opps) == 1
    assert opps[0].direction == "SHORT"


def test_scanner_halt_high_bearish_generates_short_opportunity():
    scanner = OpportunityScanner()
    nc = NewsClassification(
        symbol="PAYTM",
        scope="COMPANY",
        event_type="REGULATORY",
        sentiment="BEARISH",
        magnitude="HIGH",
        confidence=0.90,
        trading_action="HALT",
        avoid_duration_minutes=3600,
        reasoning="RBI ban",
        affected_symbols=["PAYTM"],
    )
    opps = scanner.scan(nc)
    assert len(opps) == 1
    assert opps[0].direction == "SHORT"


def test_scanner_avoid_medium_bearish_no_opportunity():
    """MEDIUM magnitude never qualifies, bearish or bullish."""
    scanner = OpportunityScanner()
    nc = NewsClassification(
        symbol="ZOMATO",
        scope="COMPANY",
        event_type="REGULATORY",
        sentiment="BEARISH",
        magnitude="MEDIUM",
        confidence=0.80,
        trading_action="AVOID",
        avoid_duration_minutes=120,
        reasoning="minor concern",
        affected_symbols=["ZOMATO"],
    )
    assert scanner.scan(nc) == []


def test_scanner_monitor_no_opportunity():
    scanner = OpportunityScanner()
    nc = _make_nc()
    assert scanner.scan(nc) == []


def test_scanner_boost_low_confidence_no_opportunity():
    scanner = OpportunityScanner()
    nc = NewsClassification(
        symbol="ZOMATO",
        scope="COMPANY",
        event_type="SENTIMENT",
        sentiment="BULLISH",
        magnitude="HIGH",
        confidence=0.50,
        trading_action="BOOST",
        avoid_duration_minutes=0,
        reasoning="Weak signal",
        affected_symbols=[],
    )
    assert scanner.scan(nc) == []


def test_scanner_boost_medium_magnitude_no_opportunity():
    scanner = OpportunityScanner()
    nc = NewsClassification(
        symbol="WIPRO",
        scope="COMPANY",
        event_type="MANAGEMENT",
        sentiment="BULLISH",
        magnitude="MEDIUM",
        confidence=0.80,
        trading_action="BOOST",
        avoid_duration_minutes=0,
        reasoning="Promotion",
        affected_symbols=[],
    )
    assert scanner.scan(nc) == []


# ---------------------------------------------------------------------------
# state_store
# ---------------------------------------------------------------------------


async def test_state_store_persists_to_repo(tmp_path):
    repo = _FakeRepo()
    store = NewsStateStore(repo, status_path=tmp_path / "news_status.json")
    nc = _make_nc("HDFCBANK", "BOOST", "HIGH", "BULLISH", 0.80)
    await store.persist(nc, source="NSE")
    assert len(repo.records) == 1
    assert repo.records[0]["symbol"] == "HDFCBANK"


async def test_state_store_writes_json(tmp_path):
    repo = _FakeRepo()
    path = tmp_path / "news_status.json"
    store = NewsStateStore(repo, status_path=path)
    nc = _make_nc("HDFCBANK", "BOOST", "HIGH", "BULLISH", 0.80)
    await store.persist(nc, source="NSE")
    assert path.exists()
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert data[0]["symbol"] == "HDFCBANK"


async def test_state_store_db_failure_still_writes_json(tmp_path):
    class _BrokenRepo:
        async def insert(self, record: dict[str, object]) -> None:
            raise RuntimeError("DB is down")

    path = tmp_path / "news_status.json"
    store = NewsStateStore(_BrokenRepo(), status_path=path)
    nc = _make_nc("ZOMATO", "MONITOR", "LOW", "NEUTRAL", 0.4)
    await store.persist(nc, source="BSE")
    assert path.exists()
