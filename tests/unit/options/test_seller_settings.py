"""Tests for the seller-first engine config loader."""

from __future__ import annotations

import json
from pathlib import Path

from src.options import seller_settings


def _write_fixture(tmp_path: Path, **overrides: object) -> Path:
    body = {
        "options_trading": {
            "enabled": False,
            "universe": {"indices": ["NIFTY", "BANKNIFTY", "FINNIFTY"]},
            "chain_fetch": {
                "tier1_interval_seconds": 30,
                "tier2_interval_seconds": 300,
                "tier3_interval_seconds": 900,
            },
            "iv_bootstrap": {"phase1_days": 90, "banknifty_vix_scaling": 1.25},
            **overrides,
        }
    }
    p = tmp_path / "options_settings.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_default_off_from_fixture(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    settings = seller_settings.load_settings(path)
    assert seller_settings.is_enabled(settings) is False


def test_enabled_true_from_fixture(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path, enabled=True)
    settings = seller_settings.load_settings(path)
    assert seller_settings.is_enabled(settings) is True


def test_universe_indices(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    settings = seller_settings.load_settings(path)
    assert seller_settings.universe_indices(settings) == ["NIFTY", "BANKNIFTY", "FINNIFTY"]


def test_ev_calibration_empty_when_absent(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    settings = seller_settings.load_settings(path)
    assert seller_settings.ev_calibration(settings) == {}


def test_ev_calibration_returns_configured_block(tmp_path: Path) -> None:
    path = _write_fixture(
        tmp_path,
        ev_calibration={
            "enabled": True,
            "bull_put_spread": {"win_rate_pct": 77.5, "avg_win": 0.5, "avg_loss": 1.5, "n": 1300},
        },
    )
    settings = seller_settings.load_settings(path)
    ev = seller_settings.ev_calibration(settings)
    assert ev["enabled"] is True
    assert ev["bull_put_spread"]["win_rate_pct"] == 77.5


def test_chain_fetch_intervals(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    settings = seller_settings.load_settings(path)
    assert seller_settings.chain_fetch_intervals(settings) == {
        "tier1": 30,
        "tier2": 300,
        "tier3": 900,
    }


def test_iv_bootstrap_phases(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    settings = seller_settings.load_settings(path)
    phases = seller_settings.iv_bootstrap_phases(settings)
    assert phases["phase1_days"] == 90
    assert phases["banknifty_vix_scaling"] == 1.25


def test_adjustment_settings_execute_rolls_is_on() -> None:
    """execute_rolls is the master switch for actually placing live roll orders
    on a delta breach. Shipped off; deliberately enabled 2026-09-15 after 4
    days of paper-mode delta-breach observation with no roll ever executed
    (see the block's own _note in options_settings.json)."""
    seller_settings.reload()
    settings = seller_settings.load_settings()
    adjustments = seller_settings.adjustment_settings(settings)
    assert adjustments.get("execute_rolls") is True
    seller_settings.reload()


def test_real_shipped_settings_parse_and_have_expected_shape() -> None:
    """Schema/parse smoke test against the real shipped config — not a default-off
    assertion, since the operator has deliberately enabled the master switch."""
    seller_settings.reload()
    settings = seller_settings.load_settings()
    assert isinstance(seller_settings.is_enabled(settings), bool)
    # FINNIFTY dropped 2026-09-28 (OTM spreads 67-195%, OI 0 -- see universe._indices_note).
    universe = set(seller_settings.universe_indices(settings))
    assert universe >= {"NIFTY", "BANKNIFTY"}
    assert "FINNIFTY" not in universe
    intervals = seller_settings.chain_fetch_intervals(settings)
    assert intervals["tier1"] > 0
    seller_settings.reload()


# ── operator close list (2026-09-28) ────────────────────────────────────────────────


def _write_settings(path, block):  # type: ignore[no-untyped-def]
    import json

    path.write_text(json.dumps({"options_trading": block}), encoding="utf-8")


def test_operator_close_ids_reads_the_file_fresh_every_call(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # load_settings() is cached for the process lifetime; an operator action must not need a
    # restart, so this accessor deliberately bypasses the cache.
    p = tmp_path / "options_settings.json"
    _write_settings(p, {"operator_actions": {"close_position_ids": ["OPT-A"]}})
    assert seller_settings.operator_close_position_ids(p) == ["OPT-A"]
    _write_settings(p, {"operator_actions": {"close_position_ids": ["OPT-A", "OPT-B"]}})
    assert seller_settings.operator_close_position_ids(p) == ["OPT-A", "OPT-B"]


def test_operator_close_ids_empty_when_absent_or_malformed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "options_settings.json"
    _write_settings(p, {})
    assert seller_settings.operator_close_position_ids(p) == []
    p.write_text("{ not json", encoding="utf-8")  # a bad edit must never break the sweep
    assert seller_settings.operator_close_position_ids(p) == []
    assert seller_settings.operator_close_position_ids(tmp_path / "missing.json") == []


def test_operator_close_ids_ignores_non_string_entries(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "options_settings.json"
    _write_settings(p, {"operator_actions": {"close_position_ids": ["OPT-A", 7, None, ""]}})
    assert seller_settings.operator_close_position_ids(p) == ["OPT-A"]
