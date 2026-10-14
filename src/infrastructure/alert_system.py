"""
Emergency Alert System — 5-tier escalating alerts.

Per docs/08-supervisor.md "Dashboard Emergency Alert System". The dashboard is
the last line of communication between the autonomous agent and the human; this
engine is the backend source of truth for active alerts.

Tiers (escalating) and their delivery policy:

  | Tier      | audio        | push | dismissible | external      |
  |-----------|--------------|------|-------------|---------------|
  | INFO      | none         | no   | yes         | no            |
  | WARNING   | chime        | no   | yes         | no            |
  | ALERT     | chime_60s    | no   | yes         | no            |
  | CRITICAL  | alarm        | yes  | yes         | no            |
  | EMERGENCY | siren        | yes  | NO          | email + sms   |

Serialization matches the dashboard contract (dashboard_react AlertItem):
  { id, ts, tier, message, acknowledged }

In-memory store. Delivery transport (Web Audio, Notification API, email/SMS) is
the dashboard/route layer's job — this engine only decides tier policy + state.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_TS_FMT = "%Y-%m-%d %H:%M:%S"


class AlertTier(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ALERT = "ALERT"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True)
class TierPolicy:
    """Delivery policy for a tier."""

    audio: str
    push: bool
    dismissible: bool
    external: bool  # email/SMS escalation


TIER_POLICY: dict[AlertTier, TierPolicy] = {
    AlertTier.INFO: TierPolicy(audio="none", push=False, dismissible=True, external=False),
    AlertTier.WARNING: TierPolicy(audio="chime", push=False, dismissible=True, external=False),
    AlertTier.ALERT: TierPolicy(audio="chime_60s", push=False, dismissible=True, external=False),
    AlertTier.CRITICAL: TierPolicy(audio="alarm", push=True, dismissible=True, external=False),
    AlertTier.EMERGENCY: TierPolicy(audio="siren", push=True, dismissible=False, external=True),
}


@dataclass
class Alert:
    """A single alert in the store."""

    id: str
    ts: str
    tier: AlertTier
    message: str
    source: str = ""
    acknowledged: bool = False
    resolved: bool = False

    @property
    def policy(self) -> TierPolicy:
        return TIER_POLICY[self.tier]

    def to_item(self) -> dict[str, Any]:
        """Serialize to the dashboard AlertItem contract."""
        return {
            "id": self.id,
            "ts": self.ts,
            "tier": self.tier.value,
            "message": self.message,
            "acknowledged": self.acknowledged,
        }


class AlertManager:
    """
    In-memory alert store with raise / acknowledge / resolve.

    Usage:
        alerts = AlertManager()
        alerts.raise_alert(AlertTier.CRITICAL, "Broker session lost", source="supervisor")
        alerts.active()            # unresolved, newest last
        alerts.acknowledge(alert_id)
        alerts.resolve(alert_id)

    `on_change`, if set, fires after every raise AND every resolve — this is the only
    store shared across the whole process (order_lifecycle.py's deferred-protection/
    GTT/repeg failures raise into the same instance as Supervisor's own invariant
    checks), but persistence to logs/alerts.json used to happen only from inside
    Supervisor's own _handle_critical(). A real CGCL "position unprotected" alert
    could sit in memory for hours without ever reaching the dashboard unless an
    unrelated invariant happened to go CRITICAL at the same time. Wiring
    on_change = Supervisor._flush_alerts closes that gap: every raise or resolve,
    from anywhere, persists immediately — including an alert disappearing off the
    dashboard the moment its underlying condition is confirmed fixed (see
    resolve_by_source and Supervisor._handle_critical/_clear_resolved_incident).
    """

    def __init__(self) -> None:
        self._alerts: dict[str, Alert] = {}
        self._order: list[str] = []  # preserves raise order
        self.on_change: Callable[[], None] | None = None

    def raise_alert(self, tier: AlertTier, message: str, source: str = "") -> Alert:
        """Create and store a new alert. Returns the Alert."""
        alert = Alert(
            id=uuid.uuid4().hex[:12],
            ts=datetime.now(_IST).strftime(_TS_FMT),
            tier=tier,
            message=message,
            source=source,
        )
        self._alerts[alert.id] = alert
        self._order.append(alert.id)
        if self.on_change is not None:
            self.on_change()
        return alert

    def acknowledge(self, alert_id: str) -> bool:
        """Mark acknowledged. EMERGENCY tier cannot be acknowledged (non-dismissible)."""
        alert = self._alerts.get(alert_id)
        if alert is None or alert.resolved:
            return False
        if not alert.policy.dismissible:
            return False
        alert.acknowledged = True
        return True

    def resolve(self, alert_id: str) -> bool:
        """Mark resolved (removed from active list)."""
        alert = self._alerts.get(alert_id)
        if alert is None:
            return False
        alert.resolved = True
        if self.on_change is not None:
            self.on_change()
        return True

    def resolve_by_source(self, source: str) -> int:
        """Resolve every active alert raised with this `source` — a symbol, trade id,
        or invariant id, matching whatever raise_alert(..., source=...) was called
        with. Used to auto-clear an alert once its underlying condition is confirmed
        fixed (a GTT re-arm succeeding, an invariant returning to OK) instead of
        leaving it sitting active on the dashboard forever. Flushes once regardless
        of how many alerts matched, not once per alert."""
        matches = [a for a in self.active() if a.source == source]
        for a in matches:
            a.resolved = True
        if matches and self.on_change is not None:
            self.on_change()
        return len(matches)

    def active(self) -> list[Alert]:
        """All unresolved alerts, in raise order."""
        return [self._alerts[i] for i in self._order if not self._alerts[i].resolved]

    def unacknowledged(self) -> list[Alert]:
        """Active alerts not yet acknowledged."""
        return [a for a in self.active() if not a.acknowledged]

    def highest_active_tier(self) -> AlertTier | None:
        """The most severe tier among active alerts, or None if quiet."""
        order = list(AlertTier)
        active = self.active()
        if not active:
            return None
        return max(active, key=lambda a: order.index(a.tier)).tier

    def get(self, alert_id: str) -> Alert | None:
        return self._alerts.get(alert_id)
