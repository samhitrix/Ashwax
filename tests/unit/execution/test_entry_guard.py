"""Tests for the pre-order chase guard."""

from __future__ import annotations

from src.execution.entry_guard import should_skip_chase


def test_inband_long_does_not_skip() -> None:
    # entry 100, sl 98 (risk 2), target 105. ltp 100.5 → drift 0.25R < 0.75R.
    skip, reason = should_skip_chase(
        entry=100.0, sl=98.0, target=105.0, ltp=100.5, direction="BUY", max_chase_r=0.75
    )
    assert skip is False
    assert reason == ""


def test_long_ltp_past_target_skips() -> None:
    skip, reason = should_skip_chase(
        entry=100.0, sl=98.0, target=105.0, ltp=105.5, direction="BUY", max_chase_r=0.75
    )
    assert skip is True
    assert reason == "ltp_past_target"


def test_long_ltp_through_sl_skips() -> None:
    skip, reason = should_skip_chase(
        entry=100.0, sl=98.0, target=105.0, ltp=97.0, direction="BUY", max_chase_r=0.75
    )
    assert skip is True
    assert reason == "ltp_through_sl"


def test_long_drift_over_cap_skips() -> None:
    # ltp 102 → drift = (102-100)/2 = 1.0R > 0.75R
    skip, reason = should_skip_chase(
        entry=100.0, sl=98.0, target=105.0, ltp=102.0, direction="BUY", max_chase_r=0.75
    )
    assert skip is True
    assert "drift" in reason


def test_short_mirror_inband() -> None:
    # short: entry 100, sl 102 (risk 2), target 95. ltp 99.5 → drift 0.25R.
    skip, reason = should_skip_chase(
        entry=100.0, sl=102.0, target=95.0, ltp=99.5, direction="SELL", max_chase_r=0.75
    )
    assert skip is False


def test_short_past_target_skips() -> None:
    skip, reason = should_skip_chase(
        entry=100.0, sl=102.0, target=95.0, ltp=94.5, direction="SELL", max_chase_r=0.75
    )
    assert skip is True
    assert reason == "ltp_past_target"


def test_degenerate_geometry_skips() -> None:
    skip, reason = should_skip_chase(
        entry=100.0, sl=100.0, target=100.0, ltp=100.0, direction="BUY", max_chase_r=0.75
    )
    assert skip is True
    assert reason == "degenerate_geometry"


# ---------------------------------------------------------------------------
# regime_blocks_direction — hard structural-regime gate
# ---------------------------------------------------------------------------


def test_regime_gate_bear_blocks_long_only():
    from src.execution.entry_guard import regime_blocks_direction

    assert regime_blocks_direction("BEAR", "LONG") is True
    assert regime_blocks_direction("STRONG_BEAR", "LONG") is True
    assert regime_blocks_direction("BEAR", "SHORT") is False
    assert regime_blocks_direction("bear", "BUY") is True  # case/alias tolerant


def test_regime_gate_bull_blocks_short_only():
    from src.execution.entry_guard import regime_blocks_direction

    assert regime_blocks_direction("BULL", "SHORT") is True
    assert regime_blocks_direction("STRONG_BULL", "SELL") is True
    assert regime_blocks_direction("BULL", "LONG") is False


def test_regime_gate_neutral_unknown_block_nothing():
    from src.execution.entry_guard import regime_blocks_direction

    for regime in ("NEUTRAL", "UNKNOWN", "", None):
        assert regime_blocks_direction(regime, "LONG") is False
        assert regime_blocks_direction(regime, "SHORT") is False


# ---------------------------------------------------------------------------
# classify_intraday_regime — day-scoped regime, refreshed per scan cycle
# ---------------------------------------------------------------------------


def test_intraday_regime_bear_needs_index_down_and_breadth():
    from src.execution.entry_guard import classify_intraday_regime

    assert classify_intraday_regime(-0.5, 0.7) == "BEAR"
    assert classify_intraday_regime(-0.5, 0.3) == "NEUTRAL"  # breadth disagrees
    assert classify_intraday_regime(-0.05, 0.7) == "NEUTRAL"  # index move too small


def test_intraday_regime_bull_mirror_and_unknown():
    from src.execution.entry_guard import classify_intraday_regime

    assert classify_intraday_regime(0.5, 0.3) == "BULL"
    assert classify_intraday_regime(0.5, 0.7) == "NEUTRAL"
    assert classify_intraday_regime(None, 0.7) == "UNKNOWN"  # no index data
    # first cycle: breadth missing → index alone classifies
    assert classify_intraday_regime(-0.5, None) == "BEAR"
    assert classify_intraday_regime(0.5, None) == "BULL"


def test_intraday_regime_config_thresholds_respected():
    from src.execution.entry_guard import classify_intraday_regime

    # stricter index threshold suppresses a small move
    assert classify_intraday_regime(-0.2, 0.7, index_move_min_pct=0.30) == "NEUTRAL"
    # stricter breadth confirm suppresses weak breadth
    assert classify_intraday_regime(-0.5, 0.55, breadth_confirm=0.60) == "NEUTRAL"
