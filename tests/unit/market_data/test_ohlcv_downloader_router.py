"""_run builds its adapter from the router, never a hard-wired KiteAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.market_data import ohlcv_downloader as dl


async def test_run_uses_router_not_kite_adapter() -> None:
    router = MagicMock()
    router.get_ohlcv = AsyncMock()
    with (
        patch("src.brokers.factory.get_market_data_router", return_value=router),
        patch.object(dl, "_refresh_credentials") as refresh,
        patch(
            "src.market_data.instrument_master.valid_nse_symbols",
            new=AsyncMock(return_value=None),
        ),
        patch.object(dl, "_download_symbol_timeframe", new=AsyncMock(return_value=True)) as d,
    ):
        await dl._run(["RELIANCE"], days=10, concurrency=1)
    refresh.assert_called_once()
    assert d.await_args.args[0] is router
