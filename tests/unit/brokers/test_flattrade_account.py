"""Flattrade account: PositionBook / Holdings / Limits (findings A3, C-10).

Sample payloads are built from the findings' field lists (values SYNTHETIC).
No network, no credentials.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from src.brokers.adapters.flattrade import adapter as adapter_mod
from src.brokers.adapters.flattrade.client import FlattradeError
from src.brokers.base import Margin

from tests.unit.brokers.fixtures.flattrade_fakes import NOW, make_adapter, quote, seq


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_mod, "_now", lambda: NOW)


def _pos(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "stat": "Ok",
        "exch": "NSE",
        "tsym": "RELIANCE-EQ",
        "token": "2885",
        "prd": "I",
        "netqty": "-10",
        "netavgprc": "2500.00",
        "daybuyqty": "0",
        "daysellqty": "10",
        "lp": "2490.00",
        "rpnl": "50.00",
        "urmtom": "100.00",
        "mult": "1",
        "prcftr": "1.000000",
        "ti": "0.05",
        "ls": "1",
    }
    row.update(fields)
    return row


# ── positions ─────────────────────────────────────────────────────────────────


async def test_positions_mapping() -> None:
    nfo = _pos(
        exch="NFO",
        tsym="NIFTY29DEC26P21050",
        token="62354",
        prd="M",
        netqty="65",
        netavgprc="101.25",
        lp="110.00",
        rpnl="0",
        urmtom="568.75",
    )
    adapter, router, _ = make_adapter({"PositionBook": [_pos(), nfo]})
    short, opt = await adapter.get_positions()
    assert (short.symbol, short.exchange, short.direction, short.quantity) == (
        "RELIANCE",
        "NSE",
        "short",
        10,
    )
    assert (short.average_price, short.last_price, short.pnl, short.product) == (
        2500.0,
        2490.0,
        150.0,
        "INTRADAY",
    )
    assert (opt.symbol, opt.direction, opt.quantity, opt.product) == (
        "NIFTY29DEC26P21050",
        "long",
        65,
        "MARGIN",
    )
    assert opt.pnl == 568.75
    assert router.payloads("PositionBook")[0]["actid"] == "FZ00000"


@pytest.mark.parametrize("prd", ["C", "I", "M"])
async def test_position_products(prd: str) -> None:
    adapter, _, _ = make_adapter({"PositionBook": [_pos(prd=prd)]})
    (p,) = await adapter.get_positions()
    assert p.product == {"C": "DELIVERY", "I": "INTRADAY", "M": "MARGIN"}[prd]


async def test_positions_no_data_is_empty_and_errors_raise() -> None:
    adapter, _, _ = make_adapter({"PositionBook": {"stat": "Not_Ok", "emsg": "No Data :  "}})
    assert await adapter.get_positions() == []
    adapter2, _, _ = make_adapter(
        {"PositionBook": {"stat": "Not_Ok", "emsg": "Server Timeout :  "}}
    )
    with pytest.raises(FlattradeError):
        await adapter2.get_positions()
    adapter3, _, _ = make_adapter({"PositionBook": {"tsym": "X"}})  # object, no stat
    with pytest.raises(FlattradeError, match="array"):
        await adapter3.get_positions()


# ── holdings ──────────────────────────────────────────────────────────────────


def _holding(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "stat": "Ok",
        "exch_tsym": [
            {
                "exch": "BSE",
                "tsym": "RELIANCE",
                "token": "500325",
                "pp": "2",
                "ti": "0.05",
                "ls": "1",
            },
            {
                "exch": "NSE",
                "tsym": "RELIANCE-EQ",
                "token": "2885",
                "pp": "2",
                "ti": "0.05",
                "ls": "1",
            },
        ],
        "holdqty": "5",
        "dpqty": "20",
        "btstqty": "0",
        "usedqty": "2",
        "upldprc": "2400.00",
    }
    row.update(fields)
    return row


async def test_holdings_mapping_prefers_nse_and_prices_from_quotes() -> None:
    adapter, router, _ = make_adapter({"Holdings": [_holding()], "GetQuotes": quote("2500.00")})
    (h,) = await adapter.get_holdings()
    assert (h.symbol, h.exchange) == ("RELIANCE", "NSE")
    assert h.quantity == 18  # dpqty 20 - usedqty 2
    assert h.t1_quantity == 5  # holdqty 5 + btstqty 0
    assert h.average_price == 2400.0
    assert h.last_price == 2500.0
    assert h.pnl == (2500.0 - 2400.0) * (18 + 5)
    assert router.payloads("Holdings")[0]["prd"] == "C"
    assert router.payloads("GetQuotes")[0] == {
        "uid": "FZ00000",
        "actid": "FZ00000",
        "exch": "NSE",
        "token": "2885",
    }


async def test_holdings_quote_refused_leaves_price_zero() -> None:
    adapter, _, _ = make_adapter(
        {"Holdings": [_holding()], "GetQuotes": {"stat": "Not_Ok", "emsg": "Invalid Input"}}
    )
    (h,) = await adapter.get_holdings()
    assert (h.last_price, h.pnl) == (0.0, 0.0)


async def test_holdings_quantity_mapping_warns_unverified_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _, _ = make_adapter({"Holdings": [_holding()], "GetQuotes": quote("2500")})
    with caplog.at_level(logging.WARNING):
        await adapter.get_holdings()
        await adapter.get_holdings()
    assert sum("flattrade_holdings_qty_unverified" in r.message for r in caplog.records) == 1


async def test_holdings_no_data_and_bad_shapes() -> None:
    adapter, _, _ = make_adapter({"Holdings": {"stat": "Not_Ok", "emsg": "No Data :  "}})
    assert await adapter.get_holdings() == []
    adapter2, _, _ = make_adapter({"Holdings": [_holding(exch_tsym=[])], "GetQuotes": quote("1")})
    with pytest.raises(FlattradeError, match="exch_tsym"):
        await adapter2.get_holdings()
    adapter3, _, _ = make_adapter({"Holdings": {"holdqty": "1"}})
    with pytest.raises(FlattradeError, match="array"):
        await adapter3.get_holdings()


# ── limits / funds ────────────────────────────────────────────────────────────


async def test_margins_from_limits() -> None:
    limits = {
        "stat": "Ok",
        "cash": "100000.00",
        "payin": "5000.00",
        "payout": "2000.00",
        "marginused": "30000.00",
        "collateral": "50000.00",
        "span": "20000",
        "expo": "10000",
        "premium": "0",
        "brokerage": "40",
        "unmtom": "-500",
        "turnover": "0",
        "pendordval": "0",
    }
    adapter, router, _ = make_adapter({"Limits": limits})
    m = await adapter.get_margins()
    assert isinstance(m, Margin)
    # payin (uncleared funds added today) is excluded (review I6); payout is subtracted
    assert m.available_cash == 100000.0 - 2000.0 - 30000.0
    assert m.used_margin == 30000.0
    assert m.collateral == 50000.0
    assert m.equity == m.available_cash + 50000.0
    assert router.payloads("Limits")[0]["actid"] == "FZ00000"


async def test_margins_payin_never_raises_available_funds() -> None:
    base = {"stat": "Ok", "cash": "1000", "marginused": "100"}
    adapter, _, _ = make_adapter({"Limits": seq(base, {**base, "payin": "99999"})})
    without, with_payin = await adapter.get_margins(), await adapter.get_margins()
    assert with_payin.available_cash == without.available_cash == 900.0
    assert with_payin.equity == without.equity


async def test_margins_missing_cash_fails_loud() -> None:
    adapter, _, _ = make_adapter({"Limits": {"stat": "Ok", "marginused": "0"}})
    with pytest.raises(FlattradeError, match="cash"):
        await adapter.get_margins()


async def test_margins_optional_fields_default_zero() -> None:
    adapter, _, _ = make_adapter({"Limits": {"stat": "Ok", "cash": "1000"}})
    m = await adapter.get_margins()
    assert (m.equity, m.available_cash, m.used_margin, m.collateral) == (1000.0, 1000.0, 0.0, 0.0)
