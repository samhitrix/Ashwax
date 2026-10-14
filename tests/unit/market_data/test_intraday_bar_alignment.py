"""Candle-boundary alignment for the intraday loops.

An ORB entry must fire seconds after the breakout bar closes, not up to two
unsynchronized 5-minute polls later. `_seconds_to_next_bar` schedules each loop
wake for the next 5m boundary plus a small settle so the just-closed candle is
available when the scan evaluates it.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from src.orchestrator import _refresh_intraday_ohlcv, _seconds_to_next_bar

_IST = ZoneInfo("Asia/Kolkata")


def _at(hh: int, mm: int, ss: int) -> datetime:
    return datetime(2026, 7, 22, hh, mm, ss, tzinfo=_IST)


def test_mid_band_waits_to_next_boundary_plus_settle():
    # 09:22:10 -> next boundary 09:25:00 (170s) + 8s settle.
    assert _seconds_to_next_bar(_at(9, 22, 10), settle_s=8.0) == 170 + 8


def test_just_after_boundary_schedules_the_following_boundary():
    # A normal wake is boundary+settle (e.g. 09:25:08). minute % 5 == 0, so the
    # next scheduled wake is the FOLLOWING boundary (09:30), not this one again.
    assert _seconds_to_next_bar(_at(9, 25, 8), settle_s=8.0) == 292 + 8


def test_ingest_lead_settle_is_shorter_than_scan():
    # Ingest leads the scan: at 09:29:55 it wakes 5s later (+2s) so 10m/15m land
    # before the scan's +8s wake reads them.
    assert _seconds_to_next_bar(_at(9, 29, 55), settle_s=2.0) == 5 + 2


def test_exact_boundary_start_targets_next_boundary():
    # 15:15 is itself a 5m boundary -> next is 15:20 (297s) + 8s.
    assert _seconds_to_next_bar(_at(15, 15, 3), settle_s=8.0) == 297 + 8


def test_never_returns_nonpositive():
    # Guarded to >= 1s so the loop can never busy-spin.
    for ss in range(0, 60, 7):
        assert _seconds_to_next_bar(_at(9, 24, ss), settle_s=8.0) >= 1.0


class _FakeRepo:
    def __init__(self, existing_ts: datetime | None) -> None:
        self._existing_ts = existing_ts
        self.upserted: list[dict] = []

    async def latest_ts(self, symbol: str, timeframe: str) -> datetime | None:
        return self._existing_ts

    async def upsert_candles(self, symbol: str, timeframe: str, rows: list[dict]) -> int:
        self.upserted.extend(rows)
        return len(rows)


class _FakeBroker:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        return self._df


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_intraday_ohlcv_drops_still_forming_candle():
    # Kite's historical_data(to_date=now()) includes the current, not-yet-closed
    # candle as the last row. The 09:30 bar only finishes at 09:35, so at 09:30:08
    # it must be dropped — trusting it would score a breakout/SL off a live print
    # that keeps changing until the bar actually closes.
    df = pd.DataFrame(
        [
            {
                "date": _at(9, 25, 0),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "volume": 10,
            },
            {
                "date": _at(9, 30, 0),
                "open": 100.5,
                "high": 103,
                "low": 100,
                "close": 102.8,
                "volume": 5,
            },
        ]
    )
    broker = _FakeBroker(df)
    repo = _FakeRepo(existing_ts=_at(9, 20, 0))
    shared = {"ohlcv_repo": repo, "watchlist": ["TESTSTOCK"], "strategy_registry": None}

    await _refresh_intraday_ohlcv(broker, shared, [("5minute", "5m")], _at(9, 30, 8))

    ts_upserted = [r["ts"] for r in repo.upserted]
    assert _at(9, 25, 0) in ts_upserted
    assert _at(9, 30, 0) not in ts_upserted


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_intraday_ohlcv_keeps_fully_closed_candle():
    df = pd.DataFrame(
        [{"date": _at(9, 25, 0), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}]
    )
    broker = _FakeBroker(df)
    repo = _FakeRepo(existing_ts=_at(9, 20, 0))
    shared = {"ohlcv_repo": repo, "watchlist": ["TESTSTOCK"], "strategy_registry": None}

    await _refresh_intraday_ohlcv(broker, shared, [("5minute", "5m")], _at(9, 30, 8))

    ts_upserted = [r["ts"] for r in repo.upserted]
    assert _at(9, 25, 0) in ts_upserted


@pytest.mark.asyncio  # type: ignore[misc]
async def test_refresh_intraday_ohlcv_shares_fetch_semaphore_across_calls():
    # _refresh_intraday_ohlcv is called from two independently-scheduled loops
    # (_ohlcv_intraday_loop for 10m/15m, _intraday_signal_scan_loop for 5m) that
    # run concurrently with no mutual gating. A fresh asyncio.Semaphore(9) on
    # every call let both loops run 9 fetches each at once (18 combined) against
    # one shared KiteAdapter rate limiter. The semaphore must be cached on
    # `shared` and reused, not recreated, across calls with the same dict.
    df = pd.DataFrame(
        [{"date": _at(9, 25, 0), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10}]
    )
    broker = _FakeBroker(df)
    repo = _FakeRepo(existing_ts=_at(9, 20, 0))
    shared = {"ohlcv_repo": repo, "watchlist": ["TESTSTOCK"], "strategy_registry": None}

    await _refresh_intraday_ohlcv(broker, shared, [("5minute", "5m")], _at(9, 30, 8))
    first_sem = shared.get("_ohlcv_fetch_sem")
    assert first_sem is not None

    await _refresh_intraday_ohlcv(broker, shared, [("10minute", "10m")], _at(9, 30, 8))
    assert shared.get("_ohlcv_fetch_sem") is first_sem
