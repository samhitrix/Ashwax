"""Tests for NightlyScanner — Phase 4 Item 3."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from src.screener.nightly_scanner import (
    NightlyScanner,
    _classify_stage,
    _score_symbol,
    _vcp_tightness,
)


def _make_prices(n: int = 100, trend: str = "up") -> pd.Series:
    rng = np.random.default_rng(42)
    base = 100.0
    if trend == "up":
        vals = base + np.arange(n) * 0.5 + rng.normal(0, 0.3, n)
    else:
        vals = base - np.arange(n) * 0.3 + rng.normal(0, 0.3, n)
    return pd.Series(vals, dtype=float)


def _make_prices_df(symbols: list[str], n: int = 100) -> pd.DataFrame:
    return pd.DataFrame({sym: _make_prices(n) for sym in symbols})


def test_scan_returns_top_20_max(tmp_path: pytest.FixtureRequest) -> None:
    log_path = tmp_path / "nightly_setups.json"  # type: ignore[operator]
    scanner = NightlyScanner(log_path=log_path)
    symbols = [f"SYM{i:03d}" for i in range(50)]
    prices_df = _make_prices_df(symbols)
    result = scanner.scan(prices_df, pd.DataFrame())
    assert len(result.top_setups) <= 20


def test_scan_empty_prices_returns_empty(tmp_path: pytest.FixtureRequest) -> None:
    log_path = tmp_path / "nightly_setups.json"  # type: ignore[operator]
    scanner = NightlyScanner(log_path=log_path)
    result = scanner.scan(pd.DataFrame(), pd.DataFrame())
    assert result.top_setups == []


def test_scan_skips_symbols_with_fewer_than_50_rows(tmp_path: pytest.FixtureRequest) -> None:
    log_path = tmp_path / "nightly_setups.json"  # type: ignore[operator]
    scanner = NightlyScanner(log_path=log_path)
    prices_df = pd.DataFrame({"SHORT": _make_prices(30)})
    result = scanner.scan(prices_df, pd.DataFrame())
    assert result.top_setups == []


def test_stage2_symbol_scores_higher_than_stage4(tmp_path: pytest.FixtureRequest) -> None:
    log_path = tmp_path / "nightly_setups.json"  # type: ignore[operator]
    scanner = NightlyScanner(log_path=log_path)
    prices_up = _make_prices(100, trend="up")
    prices_dn = _make_prices(100, trend="down")
    df = pd.DataFrame({"UP": prices_up, "DN": prices_dn})
    result = scanner.scan(df, pd.DataFrame())
    scores = {s.symbol: s.score for s in result.top_setups}
    if "UP" in scores and "DN" in scores:
        assert scores["UP"] >= scores["DN"]


def test_score_bounded_between_1_and_5() -> None:
    prices = _make_prices(100)
    score = _score_symbol("TEST", prices, pd.Series(dtype=float), rs_rank=95.0)
    assert 1.0 <= score.score <= 5.0


def test_vcp_tightness_range() -> None:
    prices = _make_prices(100)
    v = _vcp_tightness(prices)
    assert 0.0 <= v <= 10.0


def test_vcp_tightness_short_series() -> None:
    assert _vcp_tightness(pd.Series([100.0, 101.0])) == 0.0


def test_classify_stage_returns_known_stage() -> None:
    prices = _make_prices(100, trend="up")
    stage = _classify_stage(prices)
    assert stage in ("STAGE_1", "STAGE_2", "STAGE_3", "STAGE_4")


def test_computed_at_is_set(tmp_path: pytest.FixtureRequest) -> None:
    log_path = tmp_path / "nightly_setups.json"  # type: ignore[operator]
    scanner = NightlyScanner(log_path=log_path)
    prices_df = _make_prices_df(["A", "B"], n=100)
    result = scanner.scan(prices_df, pd.DataFrame())
    assert result.computed_at != ""
