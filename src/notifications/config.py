"""Notification config — config/notifications.json + Telegram credentials from .env.

Follows the codebase's established config idiom (see src/llm/providers.py:_llm_cfg,
src/brokers/factory.py): resolve config/ off __file__, json.loads, @lru_cache, and
fall back to defaults on any error rather than raising. A malformed config file must
degrade notifications, never prevent the orchestrator from booting.

Credentials live in .env, NOT in the JSON: config/ is committed, and the dashboard
settings editor writes plaintext copies into config/.backups/ on every save
(src/api/server.py:_backup_config_file).

The JSON is read once at boot and cached — same restart-to-apply semantics as
_seg_paper_mode and _strategy_execution_mode in the orchestrator.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Protocol

from src.market import sessions as _sessions

_CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "notifications.json"


class _DataclassLike(Protocol):
    __dataclass_fields__: ClassVar[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ThrottleConfig:
    modify_min_seconds_per_trade: float = 90.0
    modify_min_r_moved: float = 0.25
    modify_coalesce_seconds: float = 20.0
    modify_max_per_trade_per_day: int = 12
    global_burst: int = 5
    global_refill_seconds: float = 3.0
    dedupe_ttl_seconds: float = 600.0
    startup_quiet_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class TransportConfig:
    api_base: str = "https://api.telegram.org"
    timeout_seconds: float = 8.0
    max_retries: int = 2
    backoff_seconds: float = 1.0
    parse_mode: str = "HTML"
    alert_after_consecutive_failures: int = 5


@dataclass(frozen=True, slots=True)
class PositionUpdateConfig:
    interval_minutes: int = 15
    # config/market_hours.json schedules.position_update_notifications
    start_hhmm: str = field(
        default_factory=lambda: (
            f"{_sessions.schedule_window('position_update_notifications')[0]:%H:%M}"
        )
    )
    end_hhmm: str = field(
        default_factory=lambda: (
            f"{_sessions.schedule_window('position_update_notifications')[1]:%H:%M}"
        )
    )
    skip_when_flat: bool = True


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    enabled: bool = True
    include_paper: bool = True
    include_live: bool = True
    bot_token: str = ""
    chat_id: str = ""
    events: dict[str, bool] | None = None
    throttle: ThrottleConfig = ThrottleConfig()
    transport: TransportConfig = TransportConfig()
    position_update: PositionUpdateConfig = PositionUpdateConfig()
    eod_hhmm: str = field(default_factory=lambda: f"{_sessions.schedule_time('eod_summary'):%H:%M}")
    queue_max_size: int = 1000
    shutdown_drain_seconds: float = 2.0

    @property
    def credentials_present(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def event_enabled(self, config_key: str) -> bool:
        """Unknown keys default to enabled — a newly added EventKind should push
        rather than silently vanish because an old config file predates it."""
        if not self.events:
            return True
        return bool(self.events.get(config_key, True))

    def mode_enabled(self, is_paper: bool) -> bool:
        return self.include_paper if is_paper else self.include_live


def _sub(raw: dict[str, Any], key: str, cls: type[_DataclassLike]) -> Any:
    """Build a frozen sub-config, keeping only keys the dataclass declares so an
    unknown/stale key in the JSON cannot raise TypeError at boot."""
    block = raw.get(key)
    if not isinstance(block, dict):
        return cls()
    field_names = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: v for k, v in block.items() if k in field_names})


def _dict_or_empty(raw: dict[str, Any], key: str) -> dict[str, Any]:
    block = raw.get(key)
    return block if isinstance(block, dict) else {}


@lru_cache(maxsize=1)
def load_notification_config() -> NotificationConfig:
    raw: dict[str, Any] = {}
    try:
        raw = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    events = raw.get("events")
    queue = _dict_or_empty(raw, "queue")

    return NotificationConfig(
        enabled=bool(raw.get("enabled", True)),
        include_paper=bool(raw.get("include_paper", True)),
        include_live=bool(raw.get("include_live", True)),
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        events=dict(events) if isinstance(events, dict) else None,
        throttle=_sub(raw, "throttle", ThrottleConfig),
        transport=_sub(raw, "transport", TransportConfig),
        position_update=_sub(raw, "position_update", PositionUpdateConfig),
        eod_hhmm=f"{_sessions.schedule_time('eod_summary'):%H:%M}",
        queue_max_size=int(queue.get("max_size", 1000)),
        shutdown_drain_seconds=float(queue.get("shutdown_drain_seconds", 2.0)),
    )


def reset_config_cache() -> None:
    """Drop the cached config. For tests only — production is restart-to-apply."""
    load_notification_config.cache_clear()
