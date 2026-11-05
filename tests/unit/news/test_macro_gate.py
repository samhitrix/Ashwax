"""Macro-aware breadth-gate thresholds (src/news/context.py)."""

from __future__ import annotations

from src.news.context import macro_gate_thresholds

_BASE = 0.80
_NOW = 10_000.0


def test_no_macro_returns_base_both_sides() -> None:
    assert macro_gate_thresholds(_BASE, None, _NOW) == (_BASE, _BASE)


def test_bearish_lowers_long_block_only() -> None:
    bear_thr, bull_thr = macro_gate_thresholds(_BASE, {"bias": "BEARISH", "ts": _NOW}, _NOW)
    # LONG-block (bear) threshold drops → fires more easily; SHORT-block unchanged.
    assert bear_thr == _BASE - 0.15
    assert bull_thr == _BASE


def test_bullish_lowers_short_block_only() -> None:
    bear_thr, bull_thr = macro_gate_thresholds(_BASE, {"bias": "BULLISH", "ts": _NOW}, _NOW)
    assert bull_thr == _BASE - 0.15
    assert bear_thr == _BASE


def test_stale_macro_ignored() -> None:
    stale = {"bias": "BEARISH", "ts": _NOW - 20_000.0}  # > 14400s old
    assert macro_gate_thresholds(_BASE, stale, _NOW) == (_BASE, _BASE)
