"""Tests for the seller-desk gap stress test (options_spec.md §11.5)."""

from __future__ import annotations

from src.options.gap_risk_monitor import blocking_check, run_gap_stress_test, scenario_pnl


def test_scenario_pnl_short_gamma_loses_on_gap() -> None:
    # Short gamma (negative), no delta/vega -> loses on ANY gap (up or down).
    pnl = scenario_pnl(
        24000.0, 0.05, "up", net_delta=0.0, net_gamma=-10.0, net_vega=0.0, atm_iv=0.18
    )
    assert pnl < 0.0


def test_scenario_pnl_short_vega_loses_on_iv_spike() -> None:
    pnl = scenario_pnl(
        24000.0, 0.05, "up", net_delta=0.0, net_gamma=0.0, net_vega=-100.0, atm_iv=0.18
    )
    assert pnl < 0.0  # IV spikes on a gap -> short vega loses


def test_run_gap_stress_test_covers_10_scenarios() -> None:
    results = run_gap_stress_test(
        24000.0, net_delta=10.0, net_gamma=1.0, net_vega=50.0, atm_iv=0.18
    )
    assert len(results) == 10  # 5 magnitudes x 2 directions


def test_blocking_check_blocks_on_large_5pct_loss() -> None:
    results = run_gap_stress_test(
        24000.0, net_delta=0.0, net_gamma=-500.0, net_vega=-5000.0, atm_iv=0.18
    )
    blocked, reason = blocking_check(results, options_capital=1_000_000.0)
    assert blocked is True


def test_blocking_check_allows_small_exposure() -> None:
    results = run_gap_stress_test(24000.0, net_delta=1.0, net_gamma=0.01, net_vega=1.0, atm_iv=0.18)
    blocked, _ = blocking_check(results, options_capital=1_000_000.0)
    assert blocked is False


def test_blocking_check_banknifty_threshold_widened() -> None:
    # Tuned so the loss sits between the 1.0x and 1.3x thresholds: 5% gap loss
    # = vega x 0.35 spike x 18 vol points = 2381 x 6.3 = ~15,000+ vs limit_5
    # nifty=15,000 (blocks) / limit_5 banknifty=19,500 (doesn't).
    results = run_gap_stress_test(
        24000.0, net_delta=0.0, net_gamma=0.0, net_vega=-2700.0, atm_iv=0.18
    )
    blocked_nifty, _ = blocking_check(results, options_capital=1_000_000.0, is_banknifty=False)
    blocked_bn, _ = blocking_check(results, options_capital=1_000_000.0, is_banknifty=True)
    assert blocked_nifty is True
    assert blocked_bn is False


def test_vega_loss_uses_relative_iv_spike_in_vol_points() -> None:
    # 5% gap: IV spike 35% of an 18% IV = 6.3 vol points; short 100 rupees/vol-pt -> -630.
    pnl = scenario_pnl(
        24000.0, 0.05, "up", net_delta=0.0, net_gamma=0.0, net_vega=-100.0, atm_iv=0.18
    )
    assert abs(pnl - (-630.0)) < 1e-6
