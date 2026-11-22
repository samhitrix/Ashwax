from __future__ import annotations

import math

import pandas as pd
from scripts.compare_provider_data import compare_frames


def _df(closes: list[float], vols: list[int]) -> pd.DataFrame:
    idx = pd.date_range("2026-10-01", periods=len(closes), freq="D")
    return pd.DataFrame({"date": idx, "close": closes, "volume": vols})


def test_identical_frames_have_zero_diff() -> None:
    a = _df([100, 101, 102], [10, 20, 30])
    out = compare_frames(a, a.copy())
    assert out["common"] == 3
    assert out["max_close_diff_pct"] == 0.0


def test_close_and_volume_divergence_measured_on_common_rows_only() -> None:
    a = _df([100, 101, 102], [10, 20, 30])
    b = _df([100, 102.01, 102, 110], [10, 40, 30, 5])
    out = compare_frames(a, b)
    assert out["common"] == 3 and out["rows_b"] == 4
    assert round(out["max_close_diff_pct"], 2) == 1.0
    assert out["volume_ratio_mean"] > 1.0


def test_tz_aware_and_naive_frames_align_on_same_instants() -> None:
    # Real broker frames are tz-aware; the same instants in IST and UTC must align.
    idx_ist = pd.date_range("2026-10-01 09:15", periods=3, freq="D", tz="Asia/Kolkata")
    a = pd.DataFrame({"date": idx_ist, "close": [100.0, 101.0, 102.0], "volume": [1, 2, 3]})
    b = a.assign(date=idx_ist.tz_convert("UTC"))
    out = compare_frames(a, b)
    assert out["common"] == 3
    assert out["max_close_diff_pct"] == 0.0


def test_naive_frame_aligns_with_aware_frame_on_ist_clock() -> None:
    idx_ist = pd.date_range("2026-10-01 09:15", periods=3, freq="D", tz="Asia/Kolkata")
    aware = pd.DataFrame({"date": idx_ist, "close": [100.0, 101.0, 102.0], "volume": [1, 2, 3]})
    naive = aware.assign(date=idx_ist.tz_localize(None))  # same wall-clock, IST by convention
    out = compare_frames(naive, aware)
    assert out["common"] == 3
    assert out["max_close_diff_pct"] == 0.0


def test_zero_volume_row_excluded_from_volume_ratio_mean() -> None:
    a = _df([100, 101, 102], [0, 20, 30])
    b = _df([100, 101, 102], [5, 40, 30])
    out = compare_frames(a, b)
    assert out["volume_ratio_mean"] == 1.5  # (40/20 + 30/30) / 2; zero-volume row skipped


def test_empty_intersection_returns_nans_without_raising() -> None:
    a = _df([100, 101], [1, 2])
    b = a.assign(date=a["date"] + pd.Timedelta(days=30))
    out = compare_frames(a, b)
    assert out["common"] == 0.0
    assert math.isnan(out["max_close_diff_pct"])
    assert math.isnan(out["mean_close_diff_pct"])
    assert math.isnan(out["volume_ratio_mean"])


def test_empty_dataframe_input_returns_nan_dict() -> None:
    out = compare_frames(pd.DataFrame(), _df([100], [1]))
    assert out["rows_a"] == 0.0 and out["common"] == 0.0
    assert math.isnan(out["max_close_diff_pct"])
    out2 = compare_frames(_df([100], [1]), pd.DataFrame(columns=["date", "close", "volume"]))
    assert out2["rows_b"] == 0.0 and math.isnan(out2["volume_ratio_mean"])
