"""OHLCV rows record which provider produced them; a switch is logged, never silent."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
from src.db.repositories.ohlcv import OHLCVRepository
from src.market_data import ohlcv_downloader as dl


def _pool() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.executemany = AsyncMock()
    conn.fetchval = AsyncMock(return_value="kite")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


async def test_upsert_writes_source_column() -> None:
    pool, conn = _pool()
    repo = OHLCVRepository(pool)
    await repo.upsert_candles(
        "X",
        "5m",
        [{"ts": "2026-10-15T09:15:00+05:30", "open": 1, "high": 2, "low": 1, "close": 2}],
        source="angel_one",
    )
    sql, records = conn.executemany.await_args.args
    assert "source=EXCLUDED.source" in sql
    assert records[0][-1] == "angel_one"


async def test_latest_source_queries_newest_candle_for_symbol_timeframe() -> None:
    pool, conn = _pool()
    assert await OHLCVRepository(pool).latest_source("X", "5m") == "kite"
    sql, *params = conn.fetchval.await_args.args
    assert "symbol=$1" in sql and "timeframe=$2" in sql
    assert "ORDER BY ts DESC LIMIT 1" in sql
    assert params == ["X", "5m"]


def _df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": ["2026-10-15T09:15:00+05:30"],
            "open": [1],
            "high": [2],
            "low": [1],
            "close": [2],
            "volume": [5],
        }
    )


def _repo(prev_source: str | None, last_ts: datetime | None) -> MagicMock:
    repo = MagicMock()
    repo.latest_ts = AsyncMock(return_value=last_ts)
    repo.earliest_ts = AsyncMock(return_value=None)
    repo.latest_source = AsyncMock(return_value=prev_source)
    repo.upsert_candles = AsyncMock(return_value=1)
    return repo


def _router(served: str) -> MagicMock:
    router = MagicMock()
    router.get_ohlcv_with_source = AsyncMock(return_value=(_df(), served))
    return router


async def _run(adapter: MagicMock, repo: MagicMock) -> bool:
    return bool(
        await dl._download_symbol_timeframe(
            adapter, repo, "X", "5minute", "5m", 100, True, asyncio.Semaphore(1)
        )
    )


def _changed(caplog) -> list[str]:  # type: ignore[no-untyped-def]
    return [r.getMessage() for r in caplog.records if "ohlcv_source_changed" in r.getMessage()]


async def test_downloader_warns_when_source_changes(caplog) -> None:  # type: ignore[no-untyped-def]
    repo = _repo("kite", None)
    with caplog.at_level(logging.WARNING):
        assert await _run(_router("angel_one"), repo)
    assert _changed(caplog)
    assert repo.upsert_candles.await_args.kwargs["source"] == "angel_one"


async def test_untagged_existing_history_is_treated_as_kite(caplog) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(None, datetime.now(UTC) - timedelta(days=3))
    with caplog.at_level(logging.WARNING):
        assert await _run(_router("angel_one"), repo)
    msgs = _changed(caplog)
    assert len(msgs) == 1
    assert "from=kite" in msgs[0] and "to=angel_one" in msgs[0]


async def test_fresh_series_does_not_warn(caplog) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(None, None)
    with caplog.at_level(logging.WARNING):
        assert await _run(_router("angel_one"), repo)
    assert not _changed(caplog)


async def test_same_source_does_not_warn(caplog) -> None:  # type: ignore[no-untyped-def]
    repo = _repo("kite", None)
    with caplog.at_level(logging.WARNING):
        assert await _run(_router("kite"), repo)
    assert not _changed(caplog)


async def test_plain_adapter_writes_source_none() -> None:
    adapter = MagicMock(spec=["get_ohlcv"])
    adapter.get_ohlcv = AsyncMock(return_value=_df())
    repo = _repo(None, None)
    assert await _run(adapter, repo)
    assert repo.upsert_candles.await_args.kwargs["source"] is None
