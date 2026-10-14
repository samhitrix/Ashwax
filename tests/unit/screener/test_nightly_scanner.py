"""Unit tests for src/screener/nightly_scanner.py"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from src.screener.nightly_scanner import (
    NightlyScanner,
    _base_depth_duration,
    _classify_stage,
    _distance_to_pivot,
    _vcp_tightness,
    _volume_dryup,
)


def _prices(n: int = 100, drift: float = 0.002) -> pd.Series:
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.Series(100.0 * (1 + drift) ** np.arange(n), index=idx)


def _universe(
    symbols: dict[str, float] | None = None, n: int = 100
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if symbols is None:
        symbols = {"AAA": 0.003, "BBB": 0.001, "CCC": -0.001}
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    prices = pd.DataFrame(
        {s: 100.0 * (1 + d) ** np.arange(n) for s, d in symbols.items()}, index=idx
    )
    vols = pd.DataFrame({s: [1_000_000.0] * n for s in symbols}, index=idx)
    return prices, vols


def test_classify_stage_2():
    assert _classify_stage(_prices(100, drift=0.004)) == "STAGE_2"


def test_classify_stage_4():
    assert _classify_stage(_prices(100, drift=-0.004)) == "STAGE_4"


def test_classify_stage_short():
    assert _classify_stage(pd.Series([100.0] * 10)) == "STAGE_1"


def test_vcp_tightness_tight():
    assert _vcp_tightness(pd.Series([100.0] * 20)) == pytest.approx(10.0)


def test_vcp_tightness_wide():
    assert _vcp_tightness(pd.Series(list(range(80, 120)))) == pytest.approx(0.0)


def test_volume_dryup_low_recent():
    vols = pd.Series([1_000_000.0] * 95 + [200_000.0] * 5)
    assert _volume_dryup(vols) > 5.0


def test_volume_dryup_flat():
    assert _volume_dryup(pd.Series([1_000_000.0] * 100)) == pytest.approx(0.0, abs=0.1)


def test_distance_at_high():
    assert _distance_to_pivot(pd.Series(list(range(90, 101)))) == pytest.approx(0.0)


def test_distance_below():
    assert _distance_to_pivot(pd.Series([100.0] * 50 + [80.0])) == pytest.approx(0.20)


def test_scanner_returns_result(tmp_path):
    prices, vols = _universe()
    result = NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols)
    assert len(result.top_setups) > 0


def test_scanner_max_20(tmp_path):
    symbols = {f"S{i:02d}": 0.003 for i in range(25)}
    prices, vols = _universe(symbols)
    result = NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols)
    assert len(result.top_setups) <= 20


def test_scanner_skips_short(tmp_path):
    idx = pd.date_range("2023-01-01", periods=30, freq="B")
    prices = pd.DataFrame({"SHORT": [100.0] * 30}, index=idx)
    vols = pd.DataFrame({"SHORT": [1e6] * 30}, index=idx)
    assert NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols).top_setups == []


def test_scanner_writes_json(tmp_path):
    prices, vols = _universe()
    path = tmp_path / "ns.json"
    NightlyScanner(log_path=path).scan(prices, vols)
    data = json.loads(path.read_text())
    assert "top_setups" in data and "computed_at" in data


def test_scanner_score_bounded(tmp_path):
    prices, vols = _universe()
    for s in NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols).top_setups:
        assert 1.0 <= s.score <= 5.0


def test_scanner_high_rs_boosts_score(tmp_path):
    prices, vols = _universe({"STAR": 0.010, "AVG": 0.001})
    rs = {"STAR": 90.0, "AVG": 40.0}
    result = NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols, rs_ranks=rs)
    star = next((s for s in result.top_setups if s.symbol == "STAR"), None)
    if star:
        assert star.score > 2.0


def test_scanner_empty_volumes(tmp_path):
    prices, _ = _universe()
    result = NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, pd.DataFrame())
    assert isinstance(result.top_setups, list)


def test_base_depth_duration_flat():
    # All prices at same level → all within 15% of high → full duration
    prices = pd.Series([100.0] * 50)
    dur = _base_depth_duration(prices)
    assert dur == pytest.approx(50 / 5.0)


def test_base_depth_duration_breakout_resets():
    # Price shoots up then comes back: only the flat tail counts
    prices = pd.Series([100.0] * 30 + [200.0] * 10 + [100.0] * 10)
    dur = _base_depth_duration(prices)
    assert dur == pytest.approx(0.0)  # 100 is 50% below 200 → outside 15% threshold


def test_setup_score_has_base_depth_field(tmp_path):
    prices, vols = _universe()
    result = NightlyScanner(log_path=tmp_path / "ns.json").scan(prices, vols)
    for s in result.top_setups:
        assert hasattr(s, "base_depth_duration")
        assert s.base_depth_duration >= 0.0
