"""
Tax Report Generator -- STCG / LTCG / Speculative per TaxRuleFY2026v1.

Indian equity taxation (FY2026 rules post Budget 2024):

  Delivery trades (CNC):
    Held < 12 months  -> Short-Term Capital Gain (STCG): 20% flat
    Held >= 12 months -> Long-Term Capital Gain (LTCG): 12.5% on gains
                         above Rs 1,25,000 annual exemption

  Intraday trades (MIS):
    Speculative business income -- no flat rate; added to total income.
    Caller includes this in the ITR business income schedule.

  F&O trades: out of scope for this module.

TaxRuleFY2026v1 is the version tag for this rule set; bump when budget changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class TaxCategory(StrEnum):
    STCG = "STCG"  # Short-Term Capital Gain (delivery < 12 months)
    LTCG = "LTCG"  # Long-Term Capital Gain (delivery >= 12 months)
    SPECULATIVE = "SPECULATIVE"  # Intraday MIS -- business income


# TaxRuleFY2026v1 rates (Budget 2024 revision)
_STCG_RATE = 0.20  # 20% flat
_LTCG_RATE = 0.125  # 12.5% on gains above exemption
_LTCG_EXEMPTION = 125_000.0  # Rs 1,25,000 annual exemption per assessee
_HOLDING_PERIOD_DAYS = 365  # India uses calendar-year 12-month rule


@dataclass
class ClosedTrade:
    """Minimal fields needed for tax classification."""

    symbol: str
    product: str  # "CNC" or "MIS"
    entry_date: date
    exit_date: date
    net_pnl: float  # after all transaction costs (Rs)
    quantity: int


@dataclass
class TaxLine:
    symbol: str
    category: TaxCategory
    net_pnl: float
    tax_liability: float
    holding_days: int
    note: str


@dataclass
class TaxReport:
    """FY tax summary."""

    fy_label: str  # e.g. "FY2025-26"
    trades: list[TaxLine] = field(default_factory=list)

    stcg_gains: float = 0.0
    ltcg_gains: float = 0.0
    ltcg_gains_taxable: float = 0.0  # after Rs 1.25L exemption
    speculative_income: float = 0.0

    stcg_tax: float = 0.0
    ltcg_tax: float = 0.0
    speculative_note: str = ""  # caller adds to ITR business income schedule

    total_tax_liability: float = 0.0


def _fy_label(exit_date: date) -> str:
    """Return FY label like 'FY2025-26' for a given exit date."""
    if exit_date.month >= 4:
        return f"FY{exit_date.year}-{str(exit_date.year + 1)[2:]}"
    return f"FY{exit_date.year - 1}-{str(exit_date.year)[2:]}"


def _classify(trade: ClosedTrade) -> TaxCategory:
    if trade.product == "MIS":
        return TaxCategory.SPECULATIVE
    holding_days = (trade.exit_date - trade.entry_date).days
    if holding_days >= _HOLDING_PERIOD_DAYS:
        return TaxCategory.LTCG
    return TaxCategory.STCG


def generate_tax_report(
    trades: list[ClosedTrade],
    fy_label: str | None = None,
    ltcg_exemption: float = _LTCG_EXEMPTION,
) -> TaxReport:
    """
    Generate a tax liability summary for a list of closed trades.

    Args:
        trades: All closed trades for the FY.
        fy_label: FY label string (e.g. "FY2025-26"). Auto-detected from
                  trade exit dates if not provided.
        ltcg_exemption: LTCG annual exemption (default Rs 1,25,000).

    Returns:
        TaxReport with per-trade lines and FY totals.
    """
    if not trades:
        label = fy_label or "FY-unknown"
        return TaxReport(fy_label=label)

    label = fy_label or _fy_label(trades[0].exit_date)
    report = TaxReport(fy_label=label)

    for t in trades:
        category = _classify(t)
        holding_days = (t.exit_date - t.entry_date).days

        if category == TaxCategory.SPECULATIVE:
            report.speculative_income += t.net_pnl
            line = TaxLine(
                symbol=t.symbol,
                category=category,
                net_pnl=round(t.net_pnl, 2),
                tax_liability=0.0,
                holding_days=holding_days,
                note="Speculative business income (MIS); include in ITR business schedule",
            )
        elif category == TaxCategory.STCG:
            tax = t.net_pnl * _STCG_RATE if t.net_pnl > 0 else 0.0
            report.stcg_gains += t.net_pnl
            report.stcg_tax += tax
            line = TaxLine(
                symbol=t.symbol,
                category=category,
                net_pnl=round(t.net_pnl, 2),
                tax_liability=round(tax, 2),
                holding_days=holding_days,
                note=f"STCG @ {_STCG_RATE:.0%}",
            )
        else:  # LTCG
            report.ltcg_gains += t.net_pnl
            line = TaxLine(
                symbol=t.symbol,
                category=category,
                net_pnl=round(t.net_pnl, 2),
                tax_liability=0.0,  # computed at report level after exemption
                holding_days=holding_days,
                note=f"LTCG; held {holding_days} days",
            )

        report.trades.append(line)

    ltcg_taxable = max(0.0, report.ltcg_gains - ltcg_exemption)
    ltcg_tax = ltcg_taxable * _LTCG_RATE if ltcg_taxable > 0 else 0.0
    report.ltcg_gains_taxable = round(ltcg_taxable, 2)
    report.ltcg_tax = round(ltcg_tax, 2)

    if report.speculative_income != 0:
        report.speculative_note = (
            f"Speculative income Rs {report.speculative_income:,.2f}"
            " -- add to ITR Schedule BP (Business & Profession)"
        )

    report.stcg_gains = round(report.stcg_gains, 2)
    report.ltcg_gains = round(report.ltcg_gains, 2)
    report.speculative_income = round(report.speculative_income, 2)
    report.stcg_tax = round(report.stcg_tax, 2)
    report.total_tax_liability = round(report.stcg_tax + report.ltcg_tax, 2)

    return report
