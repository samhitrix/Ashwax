"""Tests for _load_ohlcv_prices helper and RS ranking loop guard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


class TestLoadOhlcvPrices:
    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        import src.orchestrator as orch

        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()
        assert prices is None
        assert nifty is None

    def test_returns_none_when_no_parquet_files(self, tmp_path: Path) -> None:
        import src.orchestrator as orch

        ohlcv_dir = tmp_path / "data" / "ohlcv" / "day"
        ohlcv_dir.mkdir(parents=True)
        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()
        assert prices is None

    def test_returns_dataframe_with_valid_parquet(self, tmp_path: Path) -> None:
        import pandas as pd
        import src.orchestrator as orch

        ohlcv_dir = tmp_path / "data" / "ohlcv" / "day"
        ohlcv_dir.mkdir(parents=True)
        for sym in ["RELIANCE", "INFY"]:
            pd.DataFrame({"close": [100.0 + i for i in range(30)]}).to_parquet(
                ohlcv_dir / f"{sym}.parquet"
            )

        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()

        assert prices is not None
        assert "RELIANCE" in prices.columns
        assert "INFY" in prices.columns
        assert nifty is not None

    def test_skips_files_with_too_few_rows(self, tmp_path: Path) -> None:
        import pandas as pd
        import src.orchestrator as orch

        ohlcv_dir = tmp_path / "data" / "ohlcv" / "day"
        ohlcv_dir.mkdir(parents=True)
        pd.DataFrame({"close": [100.0] * 5}).to_parquet(ohlcv_dir / "TINY.parquet")

        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()

        assert prices is None

    def test_skips_parquet_without_close_column(self, tmp_path: Path) -> None:
        import pandas as pd
        import src.orchestrator as orch

        ohlcv_dir = tmp_path / "data" / "ohlcv" / "day"
        ohlcv_dir.mkdir(parents=True)
        pd.DataFrame({"open": [100.0] * 30}).to_parquet(ohlcv_dir / "NOCLOSE.parquet")

        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()

        assert prices is None

    def test_nifty50_column_used_as_benchmark(self, tmp_path: Path) -> None:
        import pandas as pd
        import src.orchestrator as orch

        ohlcv_dir = tmp_path / "data" / "ohlcv" / "day"
        ohlcv_dir.mkdir(parents=True)
        pd.DataFrame({"close": [200.0 + i for i in range(30)]}).to_parquet(
            ohlcv_dir / "RELIANCE.parquet"
        )
        nifty_vals = [18000.0 + i for i in range(30)]
        pd.DataFrame({"close": nifty_vals}).to_parquet(ohlcv_dir / "NIFTY50.parquet")

        with patch.object(orch, "_BASE", tmp_path):
            prices, nifty = orch._load_ohlcv_prices()

        assert nifty is not None
        assert float(nifty.iloc[-1]) == pytest.approx(18029.0)
