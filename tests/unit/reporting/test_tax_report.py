"""Unit tests for src/reporting/tax_report.py"""

from __future__ import annotations

from datetime import date

import pytest
from src.reporting.tax_report import (
    ClosedTrade,
    TaxCategory,
    generate_tax_report,
)


def _trade(
    symbol: str,
    product: str,
    pnl: float,
    entry: date,
    exit_: date,
) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        product=product,
        entry_date=entry,
        exit_date=exit_,
        net_pnl=pnl,
        quantity=10,
    )


def test_empty_trades():
    report = generate_tax_report([])
    assert report.fy_label == "FY-unknown"
    assert report.total_tax_liability == 0.0


def test_stcg_classification():
    t = _trade("X", "CNC", 10_000.0, date(2025, 6, 1), date(2025, 9, 1))
    report = generate_tax_report([t])
    assert report.trades[0].category == TaxCategory.STCG
    assert report.stcg_tax == pytest.approx(2000.0)


def test_ltcg_classification():
    t = _trade("Y", "CNC", 200_000.0, date(2024, 1, 1), date(2025, 2, 1))
    report = generate_tax_report([t])
    assert report.trades[0].category == TaxCategory.LTCG
    assert report.ltcg_tax == pytest.approx(9375.0)
    assert report.ltcg_gains_taxable == pytest.approx(75_000.0)


def test_ltcg_under_exemption_no_tax():
    t = _trade("Z", "CNC", 50_000.0, date(2024, 1, 1), date(2025, 2, 1))
    report = generate_tax_report([t])
    assert report.ltcg_tax == 0.0
    assert report.ltcg_gains_taxable == 0.0


def test_speculative_income():
    t = _trade("MIS", "MIS", 5000.0, date(2025, 6, 1), date(2025, 6, 1))
    report = generate_tax_report([t])
    assert report.trades[0].category == TaxCategory.SPECULATIVE
    assert report.speculative_income == pytest.approx(5000.0)
    assert "ITR" in report.speculative_note


def test_loss_stcg_no_tax():
    t = _trade("LOSS", "CNC", -5000.0, date(2025, 6, 1), date(2025, 9, 1))
    report = generate_tax_report([t])
    assert report.stcg_tax == 0.0


def test_fy_label_auto_detected():
    t = _trade("X", "CNC", 1000.0, date(2025, 7, 1), date(2025, 11, 1))
    report = generate_tax_report([t])
    assert report.fy_label == "FY2025-26"
