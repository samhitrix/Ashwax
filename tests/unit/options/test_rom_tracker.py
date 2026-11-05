"""Tests for the seller-desk ROM tracker (options_spec.md §15)."""

from __future__ import annotations

import math

from src.options.rom_tracker import annualized_rom, rom_gate


def test_annualized_rom_computation() -> None:
    # premium=1750, margin=17500, dte=30 -> (0.10)*(365/30)*100 = 121.67%
    rom = annualized_rom(1750.0, 17500.0, 30)
    assert rom is not None
    assert math.isclose(rom, 121.666666, rel_tol=1e-4)


def test_annualized_rom_none_for_zero_margin() -> None:
    assert annualized_rom(1000.0, 0.0, 30) is None


def test_annualized_rom_none_for_zero_dte() -> None:
    assert annualized_rom(1000.0, 10000.0, 0) is None


def test_rom_gate_passes_above_minimum() -> None:
    passed, reason = rom_gate(1750.0, 17500.0, 30)
    assert passed is True
    assert "rom=" in reason


def test_rom_gate_rejects_below_minimum() -> None:
    # premium=100, margin=17500, dte=30 -> (0.0057)*(12.17)*100 = ~6.95% < 24%
    passed, reason = rom_gate(100.0, 17500.0, 30)
    assert passed is False
    assert "< min=24" in reason


def test_rom_gate_rejects_undefined() -> None:
    passed, reason = rom_gate(1000.0, 0.0, 30)
    assert passed is False
    assert "undefined" in reason
