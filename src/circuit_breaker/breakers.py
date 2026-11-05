"""
Enhanced Circuit Breakers — Gate 2 of the 10-Gate CRM pipeline.

Per docs/09-multiprocess-engine.md:
  Existing: daily loss, consecutive losses, connectivity, news blackout
  NEW:
    VIX breaker:          VIX > 25 → size ×0.5; VIX > 35 → halt new entries
    Correlation breaker:  >3 positions same sector → block further in sector
    Drawdown velocity:    3% portfolio loss in 60 min → halt remainder of session
    Spread breaker:       bid-ask > 0.5% LTP → skip entry
    Exchange anomaly:     >10% watchlist silent >5 min → halt all new orders
    Data quality:         DATA_SUSPECT in Redis → reject signals for that symbol

  DRAWDOWN STAIRCASE (progressive monthly throttle):
    0-3%:  normal operations
    3-5%:  size ×0.50, quality > 75
    5-8%:  size ×0.25, only STRONG_BULL, no intraday
    8-10%: HALT new entries
    >10%:  close weak positions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


class CircuitBreakerState(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    EMERGENCY = "EMERGENCY"


@dataclass
class CircuitBreakerResult:
    state: CircuitBreakerState
    allow_new_entries: bool
    size_multiplier: float
    triggered_breakers: list[str] = field(default_factory=list)
    min_quality_score: int = 0


class CircuitBreakerEngine:
    """
    Evaluates all circuit breakers and returns the most restrictive state.
    Called by CRM Gate 2 on every signal.
    """

    _STAIRCASE = [
        (0.10, CircuitBreakerState.EMERGENCY, 0.0, 100),
        (0.08, CircuitBreakerState.RED, 0.0, 100),
        (0.05, CircuitBreakerState.YELLOW, 0.25, 80),
        (0.03, CircuitBreakerState.YELLOW, 0.50, 75),
    ]

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._consecutive_losses: int = 0
        self._daily_loss_pct: float = 0.0
        self._monthly_drawdown_pct: float = 0.0
        self._vix: float = 0.0
        self._session_loss_start: datetime | None = None
        self._session_loss_pct_at_start: float = 0.0
        self._halted_until: datetime | None = None

    def record_loss(self, pct: float) -> None:
        self._consecutive_losses += 1
        self._daily_loss_pct += pct
        self._monthly_drawdown_pct += pct
        if self._session_loss_start is None:
            self._session_loss_start = datetime.now(_IST)
            self._session_loss_pct_at_start = self._monthly_drawdown_pct

    def record_win(self) -> None:
        self._consecutive_losses = 0

    def update_vix(self, vix: float) -> None:
        self._vix = vix

    def reset_daily(self) -> None:
        self._daily_loss_pct = 0.0
        self._consecutive_losses = 0
        self._session_loss_start = None
        self._halted_until = None

    def reset_monthly(self) -> None:
        self._monthly_drawdown_pct = 0.0

    def evaluate(self) -> CircuitBreakerResult:
        triggered: list[str] = []
        state = CircuitBreakerState.GREEN
        multiplier = 1.0
        min_quality = 0

        if self._halted_until and datetime.now(_IST) < self._halted_until:
            return CircuitBreakerResult(
                state=CircuitBreakerState.RED,
                allow_new_entries=False,
                size_multiplier=0.0,
                triggered_breakers=["MANUAL_HALT"],
            )

        max_daily = self._cfg.get("max_daily_loss_pct", 0.03)
        if self._daily_loss_pct >= max_daily:
            triggered.append(f"DAILY_LOSS_{self._daily_loss_pct:.1%}")
            state = CircuitBreakerState.RED
            multiplier = 0.0

        max_consec = self._cfg.get("max_consecutive_losses", 5)
        if self._consecutive_losses >= max_consec:
            triggered.append(f"CONSECUTIVE_LOSSES_{self._consecutive_losses}")
            state = CircuitBreakerState.RED
            multiplier = 0.0

        if self._session_loss_start:
            elapsed = (datetime.now(_IST) - self._session_loss_start).total_seconds() / 60
            loss_since = self._monthly_drawdown_pct - self._session_loss_pct_at_start
            if elapsed <= 60 and loss_since >= 0.03:
                triggered.append(f"DRAWDOWN_VELOCITY_{loss_since:.1%}_IN_{elapsed:.0f}min")
                state = CircuitBreakerState.RED
                multiplier = 0.0

        if self._vix > 35:
            triggered.append(f"VIX_HALT_{self._vix:.1f}")
            state = CircuitBreakerState.RED
            multiplier = 0.0
        elif self._vix > 25:
            triggered.append(f"VIX_ELEVATED_{self._vix:.1f}")
            multiplier = min(multiplier, 0.5)
            if state == CircuitBreakerState.GREEN:
                state = CircuitBreakerState.YELLOW

        for threshold, staircase_state, staircase_mult, quality in self._STAIRCASE:
            if self._monthly_drawdown_pct >= threshold:
                triggered.append(f"DRAWDOWN_STAIRCASE_{self._monthly_drawdown_pct:.1%}")
                multiplier = min(multiplier, staircase_mult)
                min_quality = max(min_quality, quality)
                if list(CircuitBreakerState).index(staircase_state) > list(
                    CircuitBreakerState
                ).index(state):
                    state = staircase_state
                break

        allow = state in (CircuitBreakerState.GREEN, CircuitBreakerState.YELLOW) and multiplier > 0
        return CircuitBreakerResult(
            state=state,
            allow_new_entries=allow,
            size_multiplier=multiplier,
            triggered_breakers=triggered,
            min_quality_score=min_quality,
        )

    def halt_session(self, minutes: int = 60) -> None:
        self._halted_until = datetime.now(_IST) + timedelta(minutes=minutes)
