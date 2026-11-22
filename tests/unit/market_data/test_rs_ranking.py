"""Unit tests for src/market_data/rs_ranking.py"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from src.market_data.rs_ranking import (
    RSRankingEngine,
    _is_rs_new_high,
    _percentile_rank,
    _return_12m,
    _rs_momentum,
)


def _make_prices(n: int = 260, start: float = 100.0, drift: float = 0.001) -> pd.Series:
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    prices = start * (1 + drift) ** np.arange(n)
    return pd.Series(prices, index=idx)


def _make_universe(n: int = 260, symbols: dict[str, float] | None = None) -> pd.DataFrame:
    if symbols is None:
        symbols = {"SYM_A": 0.002, "SYM_B": 0.001, "SYM_C": -0.001}
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {sym: 100.0 * (1 + drift) ** np.arange(n) for sym, drift in symbols.items()},
        index=idx,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_return_12m_positive_drift():
    assert _return_12m(_make_prices(260, drift=0.002)) > 0


def test_return_12m_flat():
    assert _return_12m(pd.Series([100.0] * 260)) == pytest.approx(0.0)


def test_return_12m_too_short():
    assert _return_12m(pd.Series([100.0])) == pytest.approx(0.0)


def test_percentile_rank_max():
    assert _percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(1.0)


def test_percentile_rank_min():
    assert _percentile_rank(1.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.2)


def test_percentile_rank_empty():
    assert _percentile_rank(1.0, []) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# RSRankingEngine
# ---------------------------------------------------------------------------


def test_rank_basic(tmp_path):
    symbols = {"HIGH": 0.005, "MID": 0.001, "LOW": -0.003}
    prices = _make_universe(symbols=symbols)
    nifty = _make_prices(drift=0.001)
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(prices, nifty)
    assert len(result.leaders) > 0
    assert len(result.blacklist) > 0


def test_rank_leaders_are_outperformers(tmp_path):
    symbols = {"STAR": 0.010, "AVERAGE": 0.001, "LOSER": -0.005}
    prices = _make_universe(symbols=symbols)
    nifty = _make_prices(drift=0.001)
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(prices, nifty)
    leader_syms = [r.symbol for r in result.leaders]
    assert "STAR" in leader_syms


def test_rank_blacklist_contains_underperformers(tmp_path):
    symbols = {"STAR": 0.010, "AVERAGE": 0.001, "LOSER": -0.005}
    prices = _make_universe(symbols=symbols)
    nifty = _make_prices(drift=0.001)
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(prices, nifty)
    blacklist_syms = [r.symbol for r in result.blacklist]
    assert "LOSER" in blacklist_syms


def test_rank_writes_json(tmp_path):
    path = tmp_path / "rs.json"
    RSRankingEngine(log_path=path).rank(_make_universe(), _make_prices())
    assert path.exists()
    data = json.loads(path.read_text())
    assert {"leaders", "improvers", "blacklist", "computed_at"} <= data.keys()


def test_rank_empty_dataframe(tmp_path):
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(pd.DataFrame(), _make_prices())
    assert result.leaders == [] and result.improvers == [] and result.blacklist == []


def test_rank_skips_short_series(tmp_path):
    idx = pd.date_range("2023-01-01", periods=10, freq="B")
    prices = pd.DataFrame({"SHORT": [100.0] * 10}, index=idx)
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(prices, _make_prices())
    assert result.leaders == []


def test_rank_llm_boost_consistent(tmp_path):
    result = RSRankingEngine(log_path=tmp_path / "rs.json").rank(_make_universe(), _make_prices())
    for r in result.leaders + result.improvers + result.blacklist:
        expected = 0.10 if r.rs_new_high else 0.0
        assert r.llm_confidence_boost == pytest.approx(expected)


def test_rs_momentum_float():
    assert isinstance(
        _rs_momentum(_make_prices(260, drift=0.002), _make_prices(260, drift=0.001)), float
    )


def test_is_rs_new_high_outperformer():
    assert _is_rs_new_high(_make_prices(260, drift=0.005), _make_prices(260, drift=0.001)) is True


def test_is_rs_new_high_underperformer():
    assert _is_rs_new_high(_make_prices(260, drift=-0.003), _make_prices(260, drift=0.001)) is False


# ---------------------------------------------------------------------------
# universe.json rs_thresholds — percentile gates (Phase 2 wiring)
# ---------------------------------------------------------------------------


def _ascending_universe(n: int = 10) -> pd.DataFrame:
    return _make_universe(symbols={f"S{i}": 0.001 * i for i in range(1, n + 1)})


def test_min_rs_to_trade_excludes_subthreshold(tmp_path):
    prices = _ascending_universe()
    nifty = _make_prices(drift=0.001)
    base = RSRankingEngine(log_path=tmp_path / "a.json").rank(prices, nifty)
    gated = RSRankingEngine(log_path=tmp_path / "b.json", min_rs_to_trade=95.0).rank(prices, nifty)
    assert len(gated.leaders) < len(base.leaders)
    assert all(r.rs_percentile >= 95.0 for r in gated.leaders)


def test_blacklist_below_unions_subthreshold(tmp_path):
    prices = _ascending_universe()
    nifty = _make_prices(drift=0.001)
    base = RSRankingEngine(log_path=tmp_path / "a.json").rank(prices, nifty)
    gated = RSRankingEngine(log_path=tmp_path / "b.json", blacklist_below=90.0).rank(prices, nifty)
    base_bl = {r.symbol for r in base.blacklist}
    gated_bl = {r.symbol for r in gated.blacklist}
    # union never un-blacklists, and adds at least one sub-threshold symbol
    assert base_bl <= gated_bl
    assert len(gated_bl) > len(base_bl)
    assert all(r.rs_percentile < 90.0 for r in gated.blacklist if r.symbol not in base_bl)
