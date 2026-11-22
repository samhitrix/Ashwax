"""Tests for the VIX-proxy IV-history bootstrap (options_spec.md §13)."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.options.bootstrap_iv_history import bootstrap, parse_vix_csv, scale_for_index

_FIXTURE = Path(__file__).parent / "fixtures" / "sample_vix_history.csv"


def _fake_pool() -> MagicMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def test_parse_vix_csv_sorted_ascending() -> None:
    rows = parse_vix_csv(_FIXTURE)
    assert len(rows) == 10
    assert rows[0] == (date(2026, 1, 1), 13.20)
    assert rows[-1] == (date(2026, 1, 14), 13.15)
    assert all(rows[i][0] < rows[i + 1][0] for i in range(len(rows) - 1))


def test_scale_for_index_nifty_unscaled() -> None:
    assert math.isclose(scale_for_index(13.20, "NIFTY"), 13.20)


def test_scale_for_index_banknifty() -> None:
    assert math.isclose(scale_for_index(13.20, "BANKNIFTY"), 13.20 * 1.25)


def test_scale_for_index_finnifty() -> None:
    assert math.isclose(scale_for_index(13.20, "FINNIFTY"), 13.20 * 1.15)


async def test_bootstrap_inserts_one_row_per_index_per_date() -> None:
    pool = _fake_pool()
    count = await bootstrap(pool, _FIXTURE, ["NIFTY", "BANKNIFTY"])
    assert count == 10 * 2

    conn = pool.acquire.return_value.__aenter__.return_value
    assert conn.execute.await_count == 20
    first_call_args = conn.execute.call_args_list[0][0]
    assert "options_vol_intel_history" in first_call_args[0]


async def test_bootstrap_first_row_has_no_ivr_expanding_window() -> None:
    pool = _fake_pool()
    await bootstrap(pool, _FIXTURE, ["NIFTY"])
    conn = pool.acquire.return_value.__aenter__.return_value
    # args = (sql, symbol, timestamp, iv_30d, iv_realized_20d, iv_realized_30d,
    #         iv_realized_60d, ivr, ivp, ...) -> ivr is index 7
    first_args = conn.execute.call_args_list[0][0]
    assert first_args[7] is None
