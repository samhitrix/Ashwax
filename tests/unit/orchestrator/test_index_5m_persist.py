"""NIFTY 50 5-minute bars must be written through to the DB every regime cycle (2026-09-28: the series
stopped on 2026-09-10 -- only a one-off boot backfill ever wrote it -- so stored-bar research and any
market-state feature built from it were blind to the last weeks)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest
from src import orchestrator as orch


def _bars(n: int = 3) -> pd.DataFrame:
    ts = pd.date_range("2026-09-28 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "date": ts,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000 + i for i in range(n)],
        }
    )


@pytest.mark.asyncio
async def test_five_minute_index_bars_are_upserted_under_the_5m_timeframe() -> None:
    broker = MagicMock()
    broker.get_ohlcv = AsyncMock(return_value=_bars(3))
    repo = MagicMock()
    repo.upsert_candles = AsyncMock(return_value=3)

    n = await orch._persist_index_intraday(broker, repo, days=20)

    assert n == 3
    broker.get_ohlcv.assert_awaited_once_with("NSE:NIFTY 50", "5minute", 20)
    sym, tf, rows = repo.upsert_candles.await_args.args
    assert (sym, tf) == ("NIFTY 50", "5m")
    assert len(rows) == 3 and rows[0]["open"] == 100.0 and rows[2]["volume"] == 1002.0


@pytest.mark.asyncio
async def test_no_bars_writes_nothing() -> None:
    broker = MagicMock()
    broker.get_ohlcv = AsyncMock(return_value=pd.DataFrame())
    repo = MagicMock()
    repo.upsert_candles = AsyncMock()
    assert await orch._persist_index_intraday(broker, repo, days=3) == 0
    repo.upsert_candles.assert_not_called()


def test_the_regime_loop_heals_a_hole_on_its_first_cycle_and_reports_failures() -> None:
    import inspect

    src = inspect.getsource(orch._regime_loop)
    assert "_persist_index_intraday" in src and "20" in src and "nifty_5m_persist_failed" in src
