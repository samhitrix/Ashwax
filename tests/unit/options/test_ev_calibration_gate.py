"""Shared EV-calibration gate (Options learning-engine PR3/PR4) — one config
block (options_settings.json ev_calibration), one mode key
(config/learning.json options_ev_calibration.mode), used by both
seller_desk.py and structure_selector.py."""

from __future__ import annotations

from src.options import ev_calibration_gate as gate


def _patch_settings(monkeypatch, block: dict) -> None:
    monkeypatch.setattr(gate._seller_settings, "ev_calibration", lambda: block)


def _patch_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(gate, "ev_calibration_mode", lambda: mode)


def test_no_calibration_entry_always_returns_heuristic(monkeypatch) -> None:
    _patch_settings(monkeypatch, {"enabled": True})
    win, loss, shadow = gate.calibrated_ev_inputs("iron_condor", "credit", 100.0, 0.0, 300.0)
    assert win == 50.0  # 0.5 * 100 heuristic
    assert loss == 150.0  # 1.5 * 100 heuristic
    assert shadow is None


def test_disabled_block_returns_heuristic_even_with_entries(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": False, "bull_put_spread": {"avg_win": 0.8, "avg_loss": 1.2}},
    )
    win, loss, shadow = gate.calibrated_ev_inputs("bull_put_spread", "credit", 100.0, 0.0, 300.0)
    assert shadow is None
    assert win == 50.0


def test_off_mode_returns_heuristic_but_still_reports_shadow(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "bull_put_spread": {"avg_win": 0.8, "avg_loss": 1.2}},
    )
    _patch_mode(monkeypatch, "off")
    win, loss, shadow = gate.calibrated_ev_inputs("bull_put_spread", "credit", 100.0, 0.0, 300.0)
    assert win == 50.0  # heuristic still drives the decision in "off"
    assert loss == 150.0
    assert shadow == {"avg_win": 80.0, "avg_loss": 120.0}  # 0.8*100, 1.2*100


def test_shadow_mode_returns_heuristic_and_reports_shadow(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "bull_put_spread": {"avg_win": 0.8, "avg_loss": 1.2}},
    )
    _patch_mode(monkeypatch, "shadow")
    win, loss, shadow = gate.calibrated_ev_inputs("bull_put_spread", "credit", 100.0, 0.0, 300.0)
    assert win == 50.0  # heuristic still drives the decision in "shadow"
    assert shadow == {"avg_win": 80.0, "avg_loss": 120.0}


def test_live_mode_with_populated_entry_uses_calibrated_value(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "bull_put_spread": {"avg_win": 0.8, "avg_loss": 1.2}},
    )
    _patch_mode(monkeypatch, "live")
    win, loss, shadow = gate.calibrated_ev_inputs("bull_put_spread", "credit", 100.0, 0.0, 300.0)
    assert win == 80.0
    assert loss == 120.0


def test_live_mode_without_entry_for_this_type_falls_back_to_heuristic(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "bull_put_spread": {"avg_win": 0.8, "avg_loss": 1.2}},
    )
    _patch_mode(monkeypatch, "live")
    win, loss, shadow = gate.calibrated_ev_inputs("iron_condor", "credit", 100.0, 0.0, 300.0)
    assert shadow is None
    assert win == 50.0  # heuristic — iron_condor has no entry in this fixture


def test_debit_structure_calibrated_value_scales_off_max_profit_and_max_loss(
    monkeypatch,
) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "long_call": {"avg_win": 0.9, "avg_loss": 1.0}},
    )
    _patch_mode(monkeypatch, "live")
    win, loss, _shadow = gate.calibrated_ev_inputs("long_call", "debit", 0.0, 1000.0, 500.0)
    assert win == 900.0  # 0.9 * max_profit_1lot
    assert loss == 500.0  # 1.0 * max_loss_1lot


def test_malformed_entry_missing_avg_loss_falls_back_to_heuristic(monkeypatch) -> None:
    _patch_settings(
        monkeypatch,
        {"enabled": True, "bull_put_spread": {"avg_win": 0.8}},  # missing avg_loss
    )
    win, loss, shadow = gate.calibrated_ev_inputs("bull_put_spread", "credit", 100.0, 0.0, 300.0)
    assert shadow is None
    assert win == 50.0


def test_ev_calibration_mode_defaults_off_when_file_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert gate.ev_calibration_mode() == "off"


def test_ev_calibration_mode_reads_config(tmp_path, monkeypatch) -> None:
    import json

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "learning.json").write_text(
        json.dumps({"options_ev_calibration": {"mode": "shadow"}})
    )
    assert gate.ev_calibration_mode() == "shadow"
