"""
Capital Source of Truth — broker is always truth; DB drift > threshold triggers reconcile.

Architecture note (from design docs):
  "Capital Source of Truth — broker is always truth; DB drift > Rs 500 -> reconcile."

This module:
  1. Compares broker-reported equity with DB-stored equity.
  2. If drift > DRIFT_THRESHOLD, flags a reconcile event (caller corrects DB).
  3. Provides a simple CapitalSnapshot for downstream use.

The broker call itself is done by the caller (BrokerInterface.get_margins());
this module only does the arithmetic and classification — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

_DRIFT_THRESHOLD = 500.0  # Rs 500 — flag reconcile if DB vs broker differs by more
_CRITICAL_DRIFT = 5000.0  # Rs 5000 — critical alert, possible data corruption


class ReconcileStatus(StrEnum):
    OK = "OK"  # drift within threshold
    RECONCILE = "RECONCILE"  # drift > threshold; caller must update DB
    CRITICAL = "CRITICAL"  # drift > critical threshold; halt new trades


@dataclass
class CapitalSnapshot:
    """Point-in-time capital state from broker."""

    broker_equity: float  # net liquidation value from broker margins API
    broker_available: float  # available cash for new trades
    broker_used_margin: float  # margin used by open positions


@dataclass
class ReconcileResult:
    status: ReconcileStatus
    broker_equity: float
    db_equity: float
    drift: float  # broker - db (positive = DB understates)
    drift_pct: float
    reason: str
    timestamp: datetime


def reconcile_capital(
    broker_snapshot: CapitalSnapshot,
    db_equity: float,
) -> ReconcileResult:
    """
    Compare broker equity with DB-stored equity and classify the drift.

    Args:
        broker_snapshot: Live snapshot from broker margins API.
        db_equity: Equity figure currently stored in DB (trades table aggregate).

    Returns:
        ReconcileResult with status and drift details.
    """
    drift = broker_snapshot.broker_equity - db_equity
    drift_pct = abs(drift) / broker_snapshot.broker_equity if broker_snapshot.broker_equity else 0.0
    now = datetime.now(_IST)

    if abs(drift) > _CRITICAL_DRIFT:
        status = ReconcileStatus.CRITICAL
        reason = (
            f"CRITICAL: broker Rs {broker_snapshot.broker_equity:,.2f}"
            f" vs DB Rs {db_equity:,.2f} — drift Rs {drift:+,.2f}"
            f" ({drift_pct:.2%}). Halt new trades, investigate immediately."
        )
    elif abs(drift) > _DRIFT_THRESHOLD:
        status = ReconcileStatus.RECONCILE
        reason = (
            f"Drift Rs {drift:+,.2f} > threshold Rs {_DRIFT_THRESHOLD:.0f}"
            f" — DB equity updated to broker value"
        )
    else:
        status = ReconcileStatus.OK
        reason = f"Drift Rs {drift:+,.2f} within Rs {_DRIFT_THRESHOLD:.0f} threshold — OK"

    return ReconcileResult(
        status=status,
        broker_equity=broker_snapshot.broker_equity,
        db_equity=db_equity,
        drift=round(drift, 2),
        drift_pct=round(drift_pct, 6),
        reason=reason,
        timestamp=now,
    )


def effective_capital(
    broker_snapshot: CapitalSnapshot,
    floor_pct: float = 0.25,
    operator_total_capital: float | None = None,
) -> float:
    """
    Tradeable capital after applying the 25% absolute floor.

    Returns the lower of:
      - broker available cash
      - (broker equity - floor_pct * operator_total_capital)
    so the system never risks below the floor even if the broker shows
    higher available margin.

    Args:
        broker_snapshot: Live broker capital snapshot.
        floor_pct: Absolute floor fraction (hardcoded 0.25 by architecture).
        operator_total_capital: Original capital baseline (from operator.json).
                                If None, uses broker_equity as baseline.
    """
    baseline = operator_total_capital or broker_snapshot.broker_equity
    floor_rupees = baseline * floor_pct
    above_floor = broker_snapshot.broker_equity - floor_rupees
    return max(0.0, min(broker_snapshot.broker_available, above_floor))
