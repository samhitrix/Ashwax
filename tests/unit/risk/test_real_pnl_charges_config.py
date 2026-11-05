"""config/charges.json overrides the statutory-charge defaults in real_pnl, per broker/segment."""

from __future__ import annotations

import json
import os

import pytest
from src.risk import real_pnl


def test_load_charges_defaults_when_file_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert real_pnl._load_charges(tmp_path / "nope.json") == real_pnl._CHARGE_DEFAULTS


def test_legacy_flat_file_overrides_only_listed_keys(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "charges.json"
    p.write_text(json.dumps({"_note": "x", "gst_rate": 0.20, "unknown_key": 1}))
    out = real_pnl._load_charges(p)
    assert out["gst_rate"] == 0.20
    assert out["brokerage_flat"] == real_pnl._CHARGE_DEFAULTS["brokerage_flat"]
    assert "unknown_key" not in out


def test_load_charges_corrupt_file_falls_back_to_defaults(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "charges.json"
    p.write_text("{not json")
    assert real_pnl._load_charges(p) == real_pnl._CHARGE_DEFAULTS


def test_per_broker_and_segment_blocks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "charges.json"
    p.write_text(
        json.dumps(
            {
                "brokers": {
                    "kite": {"equity": {"brokerage_flat": 20}},
                    "angel_one": {
                        "equity": {"brokerage_flat": 0},
                        "options": {"brokerage_flat": 15},
                    },
                }
            }
        )
    )
    assert real_pnl._load_charges(p, "angel_one")["brokerage_flat"] == 0
    assert real_pnl._load_charges(p, "kite")["brokerage_flat"] == 20
    assert real_pnl._load_charges(p, "angel_one", "options")["brokerage_flat"] == 15
    # unlisted broker/segment falls back to defaults
    assert real_pnl._load_charges(p, "upstox") == real_pnl._CHARGE_DEFAULTS
    assert real_pnl._load_charges(p, "kite", "options") == real_pnl._OPTIONS_DEFAULTS


def test_shipped_charges_json_matches_code_defaults() -> None:
    assert real_pnl._load_charges() == real_pnl._CHARGE_DEFAULTS
    assert real_pnl._load_charges(segment="options") == real_pnl._OPTIONS_DEFAULTS


def test_rates_reload_when_file_changes(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "charges.json"
    p.write_text(json.dumps({"brokers": {"kite": {"equity": {"gst_rate": 0.18}}}}))
    monkeypatch.setattr(real_pnl, "_CHARGES_PATH", p)
    real_pnl._cache.clear()
    assert real_pnl.rates("equity", "kite")["gst_rate"] == 0.18
    p.write_text(json.dumps({"brokers": {"kite": {"equity": {"gst_rate": 0.25}}}}))
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert real_pnl.rates("equity", "kite")["gst_rate"] == 0.25


def test_active_broker_paper_uses_paper_rates_broker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BROKER", "paper")
    assert real_pnl.active_broker() == "kite"
    monkeypatch.setenv("BROKER", "angel_one")
    assert real_pnl.active_broker() == "angel_one"
    assert real_pnl.active_broker(for_paper=True) == "kite"


def test_options_sell_leg_charges_hand_computed() -> None:
    c = real_pnl.options_leg_charges("SELL", 100.0, 75, "kite")  # premium value 7500
    assert c.brokerage == 20.0
    assert c.stt == pytest.approx(11.25)
    assert c.stamp_duty == 0.0
    assert c.exchange_charge == pytest.approx(2.6648, abs=1e-4)
    assert c.gst == pytest.approx(4.08)


def test_options_buy_leg_has_stamp_no_stt() -> None:
    c = real_pnl.options_leg_charges("BUY", 100.0, 75, "kite")
    assert c.stt == 0.0
    assert c.stamp_duty == pytest.approx(0.225, abs=1e-4)


def test_cost_breakdown_add_and_roundtrip() -> None:
    a = real_pnl.options_leg_charges("SELL", 100.0, 75, "kite")
    b = real_pnl.options_leg_charges("BUY", 80.0, 75, "kite")
    s = a.add(b)
    assert s.total == pytest.approx(a.total + b.total)
    assert real_pnl.CostBreakdown.from_dict(s.as_dict()).total == pytest.approx(s.total)
