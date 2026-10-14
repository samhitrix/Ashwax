"""Gross/charges/net derivation for Reports rows."""

from __future__ import annotations

import json

import pytest
from src.risk.report_costs import equity_costs, options_costs


def test_exact_breakdown_equity_gross_adds_charges_back() -> None:
    c = equity_costs(
        pnl_realized=80.0,
        charges_taxes=20.0,
        charges_breakdown=json.dumps({"brokerage": 15, "stt": 5}),
        product="MIS",
        direction="long",
        quantity=10,
        entry_price=100,
        exit_price=110,
    )
    assert (c["gross"], c["chargesTotal"], c["net"], c["estimated"]) == (100.0, 20.0, 80.0, False)


def test_no_breakdown_and_no_recorded_cost_treats_pnl_as_gross_and_estimates() -> None:
    c = equity_costs(
        pnl_realized=100.0,
        charges_taxes=None,
        charges_breakdown=None,
        product="MIS",
        direction="long",
        quantity=10,
        entry_price=100,
        exit_price=110,
    )
    assert c["gross"] == 100.0 and c["estimated"] is True
    assert c["net"] == pytest.approx(100.0 - c["chargesTotal"], abs=0.01)


def test_options_exact_keeps_gross_and_subtracts_charges() -> None:
    c = options_costs(realized_pnl=250.0, charges_breakdown={"brokerage": 80, "stt": 3.19}, legs=[])
    assert c["gross"] == 250.0 and c["net"] == pytest.approx(250.0 - 83.19) and not c["estimated"]


def test_options_estimate_from_leg_prices() -> None:
    legs = [
        {"direction": "SELL", "premium": 35.0, "exitPremium": 50.0, "quantity": 25},
        {"direction": "BUY", "premium": 25.0, "exitPremium": 50.0, "quantity": 25},
        {
            "direction": "SELL",
            "premium": 10.0,
            "exitPremium": None,
            "quantity": 25,
        },  # legacy leg skipped
    ]
    c = options_costs(realized_pnl=0.0, charges_breakdown=None, legs=legs)
    assert c["estimated"] is True and c["charges"]["brokerage"] == pytest.approx(80.0)
