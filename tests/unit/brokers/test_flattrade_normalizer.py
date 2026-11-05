"""Flattrade order / position / error normalisation and price helpers (findings A3, A7)."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.brokers.adapters.flattrade import normalizer, orders
from src.brokers.base import BrokerError

from tests.unit.brokers.fixtures.flattrade_fakes import NOW, order_row

# ── status matrix ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("OPEN", "OPEN"),
        ("open", "OPEN"),
        ("COMPLETE", "FILLED"),
        ("Complete", "FILLED"),
        ("REJECTED", "REJECTED"),
        ("REJECT", "REJECTED"),
        ("CANCELED", "CANCELLED"),
        ("CANCELLED", "CANCELLED"),
        ("cancelled", "CANCELLED"),
        ("TRIGGER_PENDING", "OPEN"),
        ("TRIGGER PENDING", "OPEN"),
        ("trigger_pending", "OPEN"),
        ("PENDING", "OPEN"),
        ("OPEN PENDING", "OPEN"),
        ("OPEN_PENDING", "OPEN"),
        ("CANCEL PENDING", "OPEN"),
        ("Cancel  Pending", "OPEN"),
        (" COMPLETE ", "FILLED"),
    ],
)
def test_status_matrix(raw: str, canonical: str) -> None:
    assert normalizer.normalize_status(raw) == canonical


@pytest.mark.parametrize("raw", ["MODIFY PENDING", "WEIRD", "", None])
def test_unknown_status_is_non_terminal_and_warns(
    raw: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        status = normalizer.normalize_status(raw)
    assert status == normalizer.UNKNOWN_STATUS
    assert status not in {"FILLED", "CANCELLED", "REJECTED"}
    assert any("flattrade_unknown_order_status" in r.message for r in caplog.records)


def test_unknown_status_on_order_with_fills_stays_unknown() -> None:
    o = normalizer.normalize_order(order_row(status="??", fillshares="4"))
    assert o.status == normalizer.UNKNOWN_STATUS


# ── normalize_order ───────────────────────────────────────────────────────────


def test_normalize_order_fields() -> None:
    o = normalizer.normalize_order(
        order_row(
            prctyp="SL-LMT",
            trantype="S",
            prd="M",
            qty="20",
            fillshares="20",
            avgprc="2501.25",
            status="COMPLETE",
            remarks="T1-SL",
        )
    )
    assert o.broker_order_id == "26101500000123"
    assert (o.symbol, o.direction, o.order_type, o.product) == ("RELIANCE", "SELL", "SL", "MARGIN")
    assert (o.quantity, o.filled_quantity, o.pending_quantity) == (20, 20, 0)
    assert (o.status, o.average_price, o.tag) == ("FILLED", 2501.25, "T1-SL")
    assert o.exchange_order_id == "1100000001"
    assert o.rejection_reason is None
    assert o.placed_at == NOW
    assert o.raw["norenordno"] == "26101500000123"


def test_normalize_order_partial_and_nfo_symbol_kept() -> None:
    o = normalizer.normalize_order(
        order_row(exch="NFO", tsym="NIFTY29DEC26P21050", qty="130", fillshares="65")
    )
    assert (o.symbol, o.status, o.pending_quantity) == ("NIFTY29DEC26P21050", "PARTIAL", 65)


def test_normalize_order_cancelled_has_no_pending() -> None:
    o = normalizer.normalize_order(order_row(status="CANCELED", qty="10", fillshares="3"))
    assert (o.status, o.filled_quantity, o.pending_quantity) == ("CANCELLED", 3, 0)


def test_normalize_order_rejection_reason() -> None:
    o = normalizer.normalize_order(order_row(status="REJECTED", rejreason="RMS:Margin Exceeds"))
    assert o.rejection_reason == "RMS:Margin Exceeds"


@pytest.mark.parametrize(
    ("prctyp", "canonical"),
    [("LMT", "LIMIT"), ("SL-LMT", "SL"), ("MKT", "MARKET"), ("SL-MKT", "SL-M")],
)
def test_normalize_order_types(prctyp: str, canonical: str) -> None:
    assert normalizer.normalize_order(order_row(prctyp=prctyp)).order_type == canonical


def test_normalize_order_bad_side_fails_loud() -> None:
    with pytest.raises(ValueError, match="trantype"):
        normalizer.normalize_order(order_row(trantype="X"))


def test_normalize_order_unparseable_time_falls_back_to_now() -> None:
    o = normalizer.normalize_order(order_row(ordenttm="not a time"))
    assert o.placed_at.tzinfo is not None


# ── normalize_position ────────────────────────────────────────────────────────


def test_normalize_position_flat_row() -> None:
    p = normalizer.normalize_position(
        {
            "exch": "NSE",
            "tsym": "SBIN-EQ",
            "prd": "C",
            "netqty": "0",
            "netavgprc": "0",
            "lp": "800",
            "rpnl": "-12.5",
            "urmtom": "0",
        }
    )
    assert (p.symbol, p.direction, p.quantity, p.pnl, p.product) == (
        "SBIN",
        "long",
        0,
        -12.5,
        "DELIVERY",
    )


def test_normalize_position_unknown_product_kept_raw() -> None:
    p = normalizer.normalize_position({"exch": "NSE", "tsym": "X-EQ", "prd": "H", "netqty": "1"})
    assert p.product == "H"


# ── normalize_error ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("emsg", "code", "retryable"),
    [
        ("Session Expired : Invalid Session Key", "TOKEN", False),
        (
            "Invalid Input : Order Recieved 11 in a current second exceeds Limit 10 for user",
            "RATE_LIMIT",
            True,
        ),
        ("Invalid Input", "INPUT", False),
        ("Order price cannot be zero", "INPUT", False),
        ("Trigger price invalid - 0.00", "INPUT", False),
        ("Server Timeout :  ", "NETWORK", True),
        ("session expired : invalid session key", "TOKEN", False),
        ("Something new", "UNKNOWN", False),
    ],
)
def test_normalize_error(emsg: str, code: str, retryable: bool) -> None:
    err = normalizer.normalize_error({"stat": "Not_Ok", "emsg": emsg})
    assert isinstance(err, BrokerError)
    assert (err.code, err.retryable) == (code, retryable)
    assert err.original_message == emsg
    assert err.original_code == "Not_Ok"


def test_normalize_error_accepts_exception_text_with_endpoint_prefix() -> None:
    err = normalizer.normalize_error(
        {"message": "PlaceOrder: Session Expired : Invalid Session Key"}
    )
    assert err.code == "TOKEN"


# ── tick rounding / band config ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("price", "tick", "mode", "expected"),
    [
        (100.03, "0.05", "up", "100.05"),
        (100.03, "0.05", "down", "100.00"),
        (100.03, "0.05", "nearest", "100.05"),
        (100.02, "0.05", "nearest", "100.00"),
        (100.05, "0.05", "up", "100.05"),  # on grid: unchanged (no float drift)
        (0.1 + 0.2, "0.01", "up", "0.30"),
        (2513.8065, "0.10", "up", "2513.90"),
        (1234.5, "0.01", "down", "1234.50"),
    ],
)
def test_round_to_tick(price: float, tick: str, mode: str, expected: str) -> None:
    assert orders.fmt(orders.round_to_tick(price, Decimal(tick), mode)) == expected


def test_band_config_default_and_validation(tmp_path: Path) -> None:
    cfg = tmp_path / "execution.json"
    assert orders.load_protection_band(tmp_path / "missing.json") == orders.DEFAULT_PROTECTION_BAND
    cfg.write_text(
        json.dumps({"flattrade": {"market_protection_band_pct": 0.01}}), encoding="utf-8"
    )
    assert orders.load_protection_band(cfg) == 0.01
    cfg.write_text(json.dumps({"other": {}}), encoding="utf-8")
    assert orders.load_protection_band(cfg) == orders.DEFAULT_PROTECTION_BAND
    for bad in (0, -0.01, 0.5, "x", True):
        cfg.write_text(
            json.dumps({"flattrade": {"market_protection_band_pct": bad}}), encoding="utf-8"
        )
        with pytest.raises(ValueError):
            orders.load_protection_band(cfg)
    cfg.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        orders.load_protection_band(cfg)


def test_shipped_execution_json_has_the_band() -> None:
    path = Path(__file__).resolve().parents[3] / "config" / "execution.json"
    block = json.loads(path.read_text(encoding="utf-8"))["flattrade"]
    assert block["market_protection_band_pct"] == 0.005
    assert "risk" in block["_note"].lower()
    assert orders.load_protection_band(path) == 0.005
