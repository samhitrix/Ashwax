"""Unit tests for src/monitoring/pnl_breakdown.py"""

from __future__ import annotations

from datetime import datetime

from src.monitoring.pnl_breakdown import TradeRecord, bucket_pnl


def _t(product: str, net_pnl: float, ts: str) -> TradeRecord:
    return TradeRecord(product=product, net_pnl=net_pnl, exit_ts=datetime.fromisoformat(ts))


def test_empty_list_returns_zeroed_buckets():
    result = bucket_pnl([])
    for key in ("MIS", "CNC", "OPTIONS", "ALL"):
        assert result[key] == {"net_pnl": 0.0, "trade_count": 0, "weekly": {}}


def test_mixed_products_bucket_correctly_and_roll_up_into_all():
    trades = [
        _t("MIS", 1000.0, "2026-08-03T15:00:00"),
        _t("MIS", -200.0, "2026-08-04T15:00:00"),
        _t("CNC", 500.0, "2026-08-05T15:00:00"),
        _t("OPTIONS", 300.0, "2026-08-06T15:00:00"),
    ]
    result = bucket_pnl(trades)

    assert result["MIS"]["net_pnl"] == 800.0
    assert result["MIS"]["trade_count"] == 2
    assert result["CNC"]["net_pnl"] == 500.0
    assert result["CNC"]["trade_count"] == 1
    assert result["OPTIONS"]["net_pnl"] == 300.0
    assert result["OPTIONS"]["trade_count"] == 1
    assert result["ALL"]["net_pnl"] == 1600.0
    assert result["ALL"]["trade_count"] == 4
    # all four trades fall in the same ISO week
    assert list(result["ALL"]["weekly"].keys()) == ["2026-W32"]


def test_nrml_and_unrecognized_product_fold_into_cnc():
    trades = [
        _t("NRML", 100.0, "2026-08-03T15:00:00"),
        _t("", 50.0, "2026-08-03T15:00:00"),
    ]
    result = bucket_pnl(trades)

    assert result["CNC"]["net_pnl"] == 150.0
    assert result["CNC"]["trade_count"] == 2
    assert result["MIS"]["trade_count"] == 0
    assert result["OPTIONS"]["trade_count"] == 0


def test_weekly_series_keyed_by_iso_week_across_boundary():
    trades = [
        _t("MIS", 100.0, "2026-08-02T10:00:00"),  # ISO week 31 (Sunday)
        _t("MIS", 200.0, "2026-08-03T10:00:00"),  # ISO week 32 (Monday)
    ]
    result = bucket_pnl(trades)

    assert result["MIS"]["weekly"] == {"2026-W31": 100.0, "2026-W32": 200.0}
