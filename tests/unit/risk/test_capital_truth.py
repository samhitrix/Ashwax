"""Unit tests for src/risk/capital_truth.py"""

from __future__ import annotations

import pytest
from src.risk.capital_truth import (
    CapitalSnapshot,
    ReconcileStatus,
    effective_capital,
    reconcile_capital,
)


def _snap(equity: float, available: float | None = None, used: float = 0.0) -> CapitalSnapshot:
    return CapitalSnapshot(
        broker_equity=equity,
        broker_available=available if available is not None else equity,
        broker_used_margin=used,
    )


def test_ok_within_threshold():
    result = reconcile_capital(_snap(200_000.0), db_equity=199_800.0)
    assert result.status == ReconcileStatus.OK


def test_reconcile_needed():
    result = reconcile_capital(_snap(200_000.0), db_equity=198_000.0)
    assert result.status == ReconcileStatus.RECONCILE
    assert result.drift == pytest.approx(2000.0)


def test_critical_drift():
    result = reconcile_capital(_snap(200_000.0), db_equity=190_000.0)
    assert result.status == ReconcileStatus.CRITICAL


def test_drift_sign_db_overstates():
    # drift = 200000 - 206000 = -6000; abs > 5000 threshold -> CRITICAL
    result = reconcile_capital(_snap(200_000.0), db_equity=206_000.0)
    assert result.drift == pytest.approx(-6000.0)
    assert result.status == ReconcileStatus.CRITICAL


def test_effective_capital_respects_floor():
    snap = _snap(200_000.0, available=190_000.0)
    cap = effective_capital(snap, floor_pct=0.25, operator_total_capital=200_000.0)
    assert cap == pytest.approx(150_000.0)


def test_effective_capital_limited_by_available():
    snap = _snap(200_000.0, available=100_000.0)
    cap = effective_capital(snap, floor_pct=0.25, operator_total_capital=200_000.0)
    assert cap == pytest.approx(100_000.0)
