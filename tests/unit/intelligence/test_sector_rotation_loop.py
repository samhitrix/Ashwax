"""Tests for sector rotation loop: sector composite price derivation."""

from __future__ import annotations

import pytest


class TestSectorStocksMap:
    def test_all_sectors_have_at_least_two_stocks(self) -> None:
        from src.orchestrator import _SECTOR_STOCKS

        for sector, stocks in _SECTOR_STOCKS.items():
            assert len(stocks) >= 2, f"{sector} needs at least 2 stocks for composite"

    def test_known_sectors_present(self) -> None:
        from src.orchestrator import _SECTOR_STOCKS

        for expected in ("BANKING", "IT", "PHARMA", "AUTO"):
            assert expected in _SECTOR_STOCKS

    def test_banking_contains_major_banks(self) -> None:
        from src.orchestrator import _SECTOR_STOCKS

        assert "HDFCBANK" in _SECTOR_STOCKS["BANKING"]
        assert "ICICIBANK" in _SECTOR_STOCKS["BANKING"]


class TestSectorCompositeDerivation:
    def test_composite_is_mean_of_available_stocks(self) -> None:
        import pandas as pd
        from src.orchestrator import _SECTOR_STOCKS

        prices = {"HDFCBANK": [100.0, 102.0, 104.0], "ICICIBANK": [200.0, 202.0, 204.0]}
        prices_df = pd.DataFrame(prices)
        available = [s for s in _SECTOR_STOCKS["BANKING"] if s in prices_df.columns]
        composite = prices_df[available].mean(axis=1)
        assert float(composite.iloc[-1]) == pytest.approx(154.0)

    def test_sector_excluded_when_fewer_than_two_stocks(self) -> None:
        import pandas as pd
        from src.orchestrator import _SECTOR_STOCKS

        prices_df = pd.DataFrame({"HDFCBANK": [100.0, 101.0, 102.0]})
        sector_series = {}
        for sector, stocks in _SECTOR_STOCKS.items():
            available = [s for s in stocks if s in prices_df.columns]
            if len(available) >= 2:
                sector_series[sector] = prices_df[available].mean(axis=1)

        assert "BANKING" not in sector_series

    def test_all_sectors_included_when_full_data(self) -> None:
        import pandas as pd
        from src.orchestrator import _SECTOR_STOCKS

        all_stocks = [s for stocks in _SECTOR_STOCKS.values() for s in stocks]
        prices_df = pd.DataFrame({s: [100.0 + i for i in range(60)] for s in all_stocks})

        sector_series = {}
        for sector, stocks in _SECTOR_STOCKS.items():
            available = [s for s in stocks if s in prices_df.columns]
            if len(available) >= 2:
                sector_series[sector] = prices_df[available].mean(axis=1)

        assert len(sector_series) == len(_SECTOR_STOCKS)

    def test_empty_prices_df_yields_no_sectors(self) -> None:
        import pandas as pd
        from src.orchestrator import _SECTOR_STOCKS

        prices_df = pd.DataFrame()
        sector_series = {}
        for sector, stocks in _SECTOR_STOCKS.items():
            available = [s for s in stocks if s in prices_df.columns]
            if len(available) >= 2:
                sector_series[sector] = prices_df[available].mean(axis=1)

        assert sector_series == {}


class TestRotationTrackingFlag:
    """universe.json sectors.rotation_tracking gates the loop (Phase 2 wiring)."""

    async def test_loop_skips_when_rotation_tracking_off(self, monkeypatch) -> None:
        import asyncio

        from src import orchestrator

        monkeypatch.setattr(
            orchestrator, "_cfg", lambda name: {"sectors": {"rotation_tracking": False}}
        )
        stop = asyncio.Event()

        async def _stop_on_sleep(_seconds: float) -> None:
            stop.set()

        monkeypatch.setattr(orchestrator.asyncio, "sleep", _stop_on_sleep)
        shared: dict = {}
        await orchestrator._sector_rotation_loop(shared, stop)
        # gated off → never classified, never published to shared state
        assert "sector_status" not in shared
