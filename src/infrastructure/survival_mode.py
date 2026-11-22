"""
Infrastructure Survival Protocol — 5 operating levels with hysteresis.

Level 1 FULL_OPS     → everything healthy, full sizing
Level 2 ELEVATED     → minor issues, size ×0.75, AMBER
Level 3 REDUCED      → DB/broker slow, swing only, size ×0.50, ORANGE
Level 4 MONITOR_ONLY → major failure / UNKNOWN regime, NO new trades, RED
Level 5 EMERGENCY    → broker unreachable / floor close, exit MIS, FLASHING RED

Hysteresis: 5 minutes stable before stepping DOWN; steps UP immediately.

Thresholds come from risk.json drawdown_staircase and circuit_breakers —
never hardcoded so operator can tune without code changes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any
from zoneinfo import ZoneInfo

_LOG = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


class OperatingLevel(IntEnum):
    FULL_OPS = 1
    ELEVATED = 2
    REDUCED = 3
    MONITOR_ONLY = 4
    EMERGENCY = 5


LEVEL_LABELS: dict[OperatingLevel, str] = {
    OperatingLevel.FULL_OPS: "FULL OPS",
    OperatingLevel.ELEVATED: "ELEVATED",
    OperatingLevel.REDUCED: "REDUCED",
    OperatingLevel.MONITOR_ONLY: "MONITOR ONLY",
    OperatingLevel.EMERGENCY: "EMERGENCY",
}

LEVEL_SIZE_MULTIPLIER: dict[OperatingLevel, float] = {
    OperatingLevel.FULL_OPS: 1.00,
    OperatingLevel.ELEVATED: 0.75,
    OperatingLevel.REDUCED: 0.50,
    OperatingLevel.MONITOR_ONLY: 0.00,
    OperatingLevel.EMERGENCY: 0.00,
}


class SurvivalMetrics:
    """Snapshot of system health. All fields optional/defaulted."""

    def __init__(
        self,
        *,
        broker_consecutive_failures: int = 0,
        db_reachable: bool = True,
        drawdown_pct: float = 0.0,
        regime: str = "BULL",
        open_critical_incidents: int = 0,
        broker_latency_ms: float = 0.0,
        vix: float = 0.0,
    ) -> None:
        self.broker_consecutive_failures = broker_consecutive_failures
        self.db_reachable = db_reachable
        self.drawdown_pct = drawdown_pct  # e.g. 0.05 = 5%
        self.regime = regime
        self.open_critical_incidents = open_critical_incidents
        self.broker_latency_ms = broker_latency_ms
        self.vix = vix


class SurvivalMode:
    """
    Tracks operating level and enforces hysteresis on step-downs.

    Args:
        risk_cfg: risk.json dict — reads drawdown_staircase + circuit_breakers.
        hysteresis_minutes: minutes stable before stepping down (default 5).
    """

    def __init__(
        self,
        risk_cfg: dict[str, Any] | None = None,
        hysteresis_minutes: int = 5,
    ) -> None:
        cfg = risk_cfg or {}
        staircase = cfg.get("drawdown_staircase", {})
        breakers = cfg.get("circuit_breakers", {})

        # Drawdown thresholds from risk.json (as fractions, config stores as %)
        self._tier1_pct = float(staircase.get("tier_1_pct", 3)) / 100  # ELEVATED
        self._tier2_pct = float(staircase.get("tier_2_pct", 5)) / 100  # REDUCED
        self._tier3_pct = float(staircase.get("tier_3_pct", 8)) / 100  # MONITOR_ONLY
        self._tier4_pct = float(staircase.get("tier_4_pct", 10)) / 100  # EMERGENCY

        # VIX thresholds from risk.json circuit_breakers
        self._vix_reduce = float(breakers.get("vix_reduce_threshold", 25))
        self._vix_halt = float(breakers.get("vix_halt_threshold", 35))

        self._hysteresis = timedelta(minutes=hysteresis_minutes)
        self._level = OperatingLevel.FULL_OPS
        self._target = OperatingLevel.FULL_OPS
        self._target_since: datetime = datetime.now(_IST)

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def level(self) -> OperatingLevel:
        return self._level

    @property
    def label(self) -> str:
        return LEVEL_LABELS[self._level]

    def size_multiplier(self) -> float:
        return LEVEL_SIZE_MULTIPLIER[self._level]

    def allow_new_trades(self) -> bool:
        return self._level < OperatingLevel.MONITOR_ONLY

    def update(self, metrics: SurvivalMetrics) -> bool:
        """Re-evaluate and possibly transition. Returns True if level changed."""
        target = self._evaluate(metrics)
        now = datetime.now(_IST)

        if target != self._target:
            self._target = target
            self._target_since = now

        # Step UP immediately — never delay on deterioration
        if target > self._level:
            old = self._level
            self._level = target
            _LOG.warning(
                "survival_level_up",
                extra={"ctx": {"from": old.name, "to": target.name}},
            )
            return True

        # Step DOWN only after hysteresis window
        if target < self._level:
            if datetime.now(_IST) - self._target_since >= self._hysteresis:
                old = self._level
                self._level = target
                _LOG.info(
                    "survival_level_down",
                    extra={"ctx": {"from": old.name, "to": target.name}},
                )
                return True
            return False

        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "level": int(self._level),
            "label": self.label,
            "size_multiplier": self.size_multiplier(),
            "allow_new_trades": self.allow_new_trades(),
            "target_level": int(self._target),
        }

    # ── Internal ───────────────────────────────────────────────────────────

    def _evaluate(self, m: SurvivalMetrics) -> OperatingLevel:
        """Pure evaluation — no state mutation."""

        # EMERGENCY: broker dead OR floor approaching OR 3+ CRITICALs OR VIX halt
        if (
            m.broker_consecutive_failures >= 3
            or m.drawdown_pct >= self._tier4_pct
            or m.open_critical_incidents >= 3
            or m.vix >= self._vix_halt
        ):
            return OperatingLevel.EMERGENCY

        # MONITOR_ONLY: any broker failure OR UNKNOWN regime OR tier 3 drawdown OR DB down
        if (
            m.broker_consecutive_failures >= 1
            or m.regime == "UNKNOWN"
            or m.drawdown_pct >= self._tier3_pct
            or not m.db_reachable
            or m.open_critical_incidents >= 2
        ):
            return OperatingLevel.MONITOR_ONLY

        # REDUCED: tier 2 drawdown OR broker slow
        if m.drawdown_pct >= self._tier2_pct or m.broker_latency_ms > 2000:
            return OperatingLevel.REDUCED

        # ELEVATED: tier 1 drawdown OR VIX above reduce threshold OR minor latency / 1 CRITICAL
        if (
            m.drawdown_pct >= self._tier1_pct
            or m.vix >= self._vix_reduce
            or m.broker_latency_ms > 800
            or m.open_critical_incidents >= 1
        ):
            return OperatingLevel.ELEVATED

        return OperatingLevel.FULL_OPS
