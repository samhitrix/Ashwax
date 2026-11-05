"""Process-wide way for ANY module to put a CRITICAL/ALERT in front of the operator.

Two surfaces, both fed from one call:
  * the dashboard Alerts tab (the supervisor's AlertManager -> logs/alerts.json), and
  * Telegram (notifications.notify, LLM_HEALTH formatter).

Added 2026-09-24: LLM provider failures, exhausted budgets, oversized prompts and duplicate
calls only ever reached a log file, so the Alerts tab stayed empty while the system burned
tokens. Deep modules (src/llm/providers.py) have no handle on the AlertManager, hence this
module-level hook, configured once at orchestrator boot. Deduplicated per `source` so a
flapping condition alerts once per window, not once per call. Never raises.
"""

from __future__ import annotations

import html
import re
import time
from collections import deque
from typing import Any

_MANAGER: Any = None
_LAST_RAISED: dict[str, float] = {}
# Every Telegram message is mirrored onto the Alerts tab; keep only the newest N active so
# alerts.json (rewritten on every change) cannot grow without bound over a long day.
MIRROR_CAP = 300
_MIRRORED: deque[str] = deque()


def configure(alert_manager: Any) -> None:
    """Attach the process's AlertManager (call once at boot)."""
    global _MANAGER
    _MANAGER = alert_manager


def raise_alert(
    message: str,
    *,
    source: str,
    tier: str = "CRITICAL",
    dedupe_seconds: float = 600.0,
    push: bool = True,
    push_title: str = "ALERT",
) -> bool:
    """Raise `message` on the Alerts tab (+ Telegram when `push`). Returns True if raised,
    False if suppressed as a duplicate of `source` inside `dedupe_seconds`."""
    try:
        now = time.monotonic()
        last = _LAST_RAISED.get(source)
        if last is not None and now - last < dedupe_seconds:
            return False
        _LAST_RAISED[source] = now
        if _MANAGER is not None:
            from src.infrastructure.alert_system import AlertTier

            _MANAGER.raise_alert(AlertTier(tier), message, source=source)
        if push:
            from src.notifications import EventKind, notify

            notify(
                EventKind.LLM_HEALTH,
                is_paper=True,
                trigger=push_title,
                overall="FAIL",
                failures=[message],
                _dashboard_done=True,  # already on the Alerts tab: the notifier must not mirror it again
            )
        return True
    except Exception:  # noqa: BLE001 — alerting must never break the caller
        return False


def mirror_notification(text_html: str, *, source: str, tier: str = "ALERT") -> bool:
    """Put a message that was just sent to Telegram on the dashboard Alerts tab as plain text.
    Keeps only the newest MIRROR_CAP mirrored alerts active (older ones are auto-resolved).
    Never raises."""
    try:
        if _MANAGER is None:
            return False
        from src.infrastructure.alert_system import AlertTier

        plain = html.unescape(re.sub(r"<[^>]+>", "", text_html)).strip()
        alert = _MANAGER.raise_alert(AlertTier(tier), plain, source=source)
        _MIRRORED.append(alert.id)
        while len(_MIRRORED) > MIRROR_CAP:
            _MANAGER.resolve(_MIRRORED.popleft())
        return True
    except Exception:  # noqa: BLE001 — must never affect Telegram delivery
        return False


def _reset_for_testing() -> None:
    global _MANAGER
    _MANAGER = None
    _LAST_RAISED.clear()
    _MIRRORED.clear()
