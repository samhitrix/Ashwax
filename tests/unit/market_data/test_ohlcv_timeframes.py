"""universe.json ohlcv.intraday_days_rolling drives intraday backfill depth (Phase 2 wiring)."""

from __future__ import annotations

from src.market_data.ohlcv_downloader import build_timeframes


def _intraday_depths(tfs: list[tuple[str, str, int, bool]]) -> dict[str, int]:
    return {tf_key: depth for _, tf_key, depth, is_intraday in tfs if is_intraday}


def test_intraday_days_rolling_drives_5m_10m_depth():
    depths = _intraday_depths(build_timeframes(60))
    assert depths["5m"] == 60
    assert depths["10m"] == 60


def test_intraday_depth_capped_at_kite_hard_limit():
    # 5m/10m Kite hard limit is 100 days; a larger config must not exceed it.
    depths = _intraday_depths(build_timeframes(500))
    assert depths["5m"] == 100
    assert depths["10m"] == 100


def test_15m_keeps_its_own_200_day_ceiling():
    depths = _intraday_depths(build_timeframes(60))
    assert depths["15m"] == 200


def test_default_reads_config_value():
    # config ships intraday_days_rolling=100 → 5m/10m == 100, identical to legacy literals
    depths = _intraday_depths(build_timeframes())
    assert depths["5m"] == 100
    assert depths["10m"] == 100
