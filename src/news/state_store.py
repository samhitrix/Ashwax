"""
News State Store â€” persists NewsClassification to DB + JSON.

Writes:
  - DB: news_events table via NewsRepository
  - JSON: logs/news_status.json (atomic write, latest 100 entries)

Inject NewsRepository for testability.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from src.infrastructure.logger import get_logger
from src.news.classifier import NewsClassification

logger = get_logger(__name__)
_IST = ZoneInfo("Asia/Kolkata")
_STATUS_PATH = Path("logs/news_status.json")
_MAX_JSON_ENTRIES = 100


class NewsRepository(Protocol):
    async def insert(self, record: dict[str, object]) -> None: ...


class NewsStateStore:
    """Persists a NewsClassification to DB and logs/news_status.json."""

    def __init__(
        self,
        repo: NewsRepository,
        status_path: Path = _STATUS_PATH,
        earnings_pre_hours: int = 24,
        earnings_post_hours: int = 4,
        rbi_policy_blackout: bool = True,
        budget_blackout: bool = True,
    ) -> None:
        self._repo = repo
        self._status_path = status_path
        self._earnings_pre_hours = earnings_pre_hours
        self._earnings_post_hours = earnings_post_hours
        self._rbi_policy_blackout = rbi_policy_blackout
        self._budget_blackout = budget_blackout

    async def persist(self, nc: NewsClassification, source: str, headline: str = "") -> None:
        record = {
            "headline": headline,
            "symbol": nc.symbol,
            "scope": nc.scope,
            "event_type": nc.event_type,
            "sentiment": nc.sentiment,
            "magnitude": nc.magnitude,
            "confidence": nc.confidence,
            "trading_action": nc.trading_action,
            "avoid_duration_minutes": nc.avoid_duration_minutes,
            "reasoning": nc.reasoning,
            "affected_symbols": nc.affected_symbols,
            "source": source,
            "created_at": datetime.now(_IST).isoformat(),
        }
        try:
            await self._repo.insert(record)
        except Exception as _e:
            logger.error("db_insert_failed symbol=%s err=%s", nc.symbol, _e)

        self._update_json(record)

    def _update_json(self, record: dict[str, object]) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, object]] = []
        if self._status_path.exists():
            try:
                existing = json.loads(self._status_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []

        existing.append(record)
        existing = existing[-_MAX_JSON_ENTRIES:]

        tmp = self._status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        try:
            tmp.replace(self._status_path)
        except PermissionError:
            self._status_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            tmp.unlink(missing_ok=True)
