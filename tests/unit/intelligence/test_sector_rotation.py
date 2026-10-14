"""Unit tests for src/intelligence/sector_rotation.py"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from src.intelligence.sector_rotation import (
    SectorRotationTracker,
    SectorStatus,
    _classify,
    _compute_rs_4w,
    _compute_volume_trend,
    _is_above_ma,
)


def _sector_prices(n: int = 100, sectors: dict[str, float] | None = None) -> pd.DataFrame:
    if sectors is None:
        sectors = {"BANKING": 0.003, "IT": -0.002, "AUTO": 0.001}
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {name: 100.0 * (1 + drift) ** np.arange(n) for name, drift in sectors.items()},
        index=idx,
    )


def _nifty(n: int = 100, drift: float = 0.001) -> pd.Series:
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.Series(100.0 * (1 + drift) ** np.arange(n), index=idx)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_classify_leading():
    assert _classify(0.02, 100.0, True) == SectorStatus.LEADING


def test_classify_emerging():
    assert _classify(0.02, -50.0, False) == SectorStatus.EMERGING


def test_classify_declining():
    assert _classify(-0.02, -50.0, False) == SectorStatus.DECLINING


def test_classify_lagging():
    assert _classify(-0.02, 100.0, True) == SectorStatus.LAGGING


def test_is_above_ma_true():
    prices = pd.Series(list(range(1, 102)))
    assert _is_above_ma(prices, 50) is True


def test_is_above_ma_false():
    prices = pd.Series(list(range(101, 0, -1)))
    assert _is_above_ma(prices, 50) is False


def test_compute_rs_4w_outperformer():
    assert _compute_rs_4w(_nifty(100, drift=0.005), _nifty(100, drift=0.001)) > 0


def test_compute_rs_4w_underperformer():
    assert _compute_rs_4w(_nifty(100, drift=-0.003), _nifty(100, drift=0.001)) < 0


def test_compute_volume_trend_rising():
    assert _compute_volume_trend(pd.Series(list(range(1, 22)))) > 0


def test_compute_volume_trend_falling():
    assert _compute_volume_trend(pd.Series(list(range(21, 0, -1)))) < 0


# ---------------------------------------------------------------------------
# SectorRotationTracker
# ---------------------------------------------------------------------------


def test_tracker_basic(tmp_path):
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(_sector_prices(), _nifty())
    assert len(result.sectors) > 0


def test_tracker_leading_confidence_boost(tmp_path):
    prices = _sector_prices(sectors={"BANKING": 0.010})
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(
        prices, _nifty(drift=0.001)
    )
    for r in result.sectors:
        if r.status == SectorStatus.LEADING:
            assert r.confidence_delta == pytest.approx(0.05)
            assert r.blocks_entries is False


def test_tracker_declining_blocks_entries(tmp_path):
    prices = _sector_prices(sectors={"REALTY": -0.010})
    vols = pd.DataFrame(
        {"REALTY": list(range(100, 0, -1))},
        index=pd.date_range("2023-01-01", periods=100, freq="B"),
    )
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(
        prices, _nifty(drift=0.001), sector_volumes=vols
    )
    realty = next((r for r in result.sectors if r.name == "REALTY"), None)
    if realty and realty.status == SectorStatus.DECLINING:
        assert realty.blocks_entries is True


def test_tracker_skips_short_series(tmp_path):
    idx = pd.date_range("2023-01-01", periods=30, freq="B")
    prices = pd.DataFrame({"SHORT": [100.0] * 30}, index=idx)
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(prices, _nifty())
    assert result.sectors == []


def test_tracker_writes_json(tmp_path):
    path = tmp_path / "sr.json"
    SectorRotationTracker(log_path=path).update(_sector_prices(), _nifty())
    assert path.exists()
    data = json.loads(path.read_text())
    assert "sectors" in data and "computed_at" in data


def test_tracker_without_volumes(tmp_path):
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(_sector_prices(), _nifty())
    assert isinstance(result.sectors, list)


def test_tracker_valid_enum_status(tmp_path):
    result = SectorRotationTracker(log_path=tmp_path / "sr.json").update(_sector_prices(), _nifty())
    valid = {s.value for s in SectorStatus}
    for r in result.sectors:
        assert r.status.value in valid
