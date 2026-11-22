"""
News Poller â€” multi-source async RSS + API polling.

Sources (priority order):
  1. NSE Corporate Announcements API (60s poll)
  2. BSE Corporate Filings (60s poll)
  3. Economic Times Markets RSS (120s poll)
  4. Moneycontrol RSS (120s poll)
  5. Business Standard RSS (120s poll)

Each source returns a list[NewsItem]. Caller deduplicates via Deduplicator.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger

_IST = ZoneInfo("Asia/Kolkata")
logger = get_logger(__name__)


@dataclass
class NewsItem:
    source: str
    headline: str
    url: str
    raw_symbols: list[str]  # symbols extracted from headline/content
    published_at: datetime
    content: str = ""
    dedup_hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.dedup_hash = hashlib.sha256((self.headline + self.url).encode()).hexdigest()


class HttpClient(Protocol):
    """Minimal async HTTP interface â€” inject real aiohttp/httpx in production."""

    async def get_text(self, url: str) -> str: ...


class NewsPoller:
    """
    Polls all configured news sources.

    Usage:
        poller = NewsPoller(http_client)
        items = await poller.poll_rss("ET_MARKETS", url)
    """

    def __init__(
        self,
        http_client: HttpClient,
        poll_interval_seconds: int = 300,
        dedup_window_minutes: int = 60,
    ) -> None:
        self._http = http_client
        self._poll_interval_seconds = poll_interval_seconds
        self._dedup_window_minutes = dedup_window_minutes

    async def poll_rss(self, source_name: str, url: str) -> list[NewsItem]:
        """Fetch and parse an RSS feed. Returns empty list on any error."""
        try:
            xml_text = await self._http.get_text(url)
            return _parse_rss(source_name, xml_text)
        except Exception:
            logger.warning("rss_poll_failed source=%s url=%s", source_name, url)
            return []

    async def poll_nse(self, api_response: str) -> list[NewsItem]:
        """Parse NSE corporate announcements JSON response."""
        import json

        items: list[NewsItem] = []
        try:
            data = json.loads(api_response)
            announcements = data if isinstance(data, list) else data.get("data", [])
            for ann in announcements:
                headline = ann.get("desc", "") or ann.get("subject", "")
                symbol = ann.get("symbol", "")
                url = ann.get("attchmntFile", "") or ann.get("filingId", "")
                if not headline:
                    continue
                # desc is a generic category shared by every company; attchmntText is the
                # actual announcement summary and is what the classifier needs to see.
                content = ann.get("attchmntText", "") or headline
                items.append(
                    NewsItem(
                        source="NSE",
                        headline=headline,
                        url=str(url),
                        raw_symbols=[symbol] if symbol else [],
                        published_at=datetime.now(_IST),
                        content=f"{headline}. {content}" if content != headline else headline,
                    )
                )
        except Exception:
            logger.warning("nse_parse_failed")
        return items

    async def poll_bse(self, api_response: str) -> list[NewsItem]:
        """Parse BSE corporate filings JSON response."""
        import json

        items: list[NewsItem] = []
        try:
            data = json.loads(api_response)
            filings = data if isinstance(data, list) else data.get("Table", [])
            for f in filings:
                headline = f.get("NEWSSUB", "") or f.get("HEADLINE", "")
                symbol = f.get("SCRIP_CD", "") or f.get("scripCd", "")
                if not headline:
                    continue
                items.append(
                    NewsItem(
                        source="BSE",
                        headline=headline,
                        url=str(f.get("ATTACHMENTNAME", "")),
                        raw_symbols=[str(symbol)] if symbol else [],
                        published_at=datetime.now(_IST),
                        content=headline,
                    )
                )
        except Exception:
            logger.warning("bse_parse_failed")
        return items


def _parse_rss(source_name: str, xml_text: str) -> list[NewsItem]:
    """Parse RSS XML into NewsItem list."""
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 â€” RSS from hardcoded trusted sources only
        channel = root.find("channel")
        if channel is None:
            return items
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title:
                continue
            items.append(
                NewsItem(
                    source=source_name,
                    headline=title,
                    url=link,
                    raw_symbols=[],
                    published_at=datetime.now(_IST),
                    content=(item.findtext("description") or "").strip(),
                )
            )
    except ET.ParseError:
        logger.warning("rss_parse_error source=%s", source_name)
    return items
