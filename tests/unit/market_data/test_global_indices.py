"""Global index ticker feed: row shaping, caching, and stale fallback on failure."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from src.market_data import global_indices as gi


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    gi._cache.update(ts=0.0, rows=[])


def _meta(price: float, prev: float, start: int = 100, end: int = 200) -> dict[str, Any]:
    return {
        "regularMarketPrice": price,
        "chartPreviousClose": prev,
        "currentTradingPeriod": {"regular": {"start": start, "end": end}},
    }


def _run(**kw: Any) -> dict[str, Any]:
    return asyncio.run(gi.get_global_indices(**kw))


def test_rows_have_change_pct_and_open_flag() -> None:
    async def fetch(_sym: str) -> dict[str, Any]:
        return _meta(110.0, 100.0)

    out = _run(fetch_one=fetch, now_epoch=lambda: 150.0)
    assert out["stale"] is False and out["delayed"] is True
    assert len(out["rows"]) == len(gi.GLOBAL_INDICES)
    assert out["rows"][0]["change_pct"] == 10.0
    assert out["rows"][0]["open"] is True


def test_closed_outside_regular_session() -> None:
    async def fetch(_sym: str) -> dict[str, Any]:
        return _meta(100.0, 100.0)

    out = _run(fetch_one=fetch, now_epoch=lambda: 500.0)
    assert out["rows"][0]["open"] is False


def test_second_call_within_ttl_uses_cache() -> None:
    calls = 0

    async def fetch(_sym: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _meta(100.0, 100.0)

    _run(fetch_one=fetch)
    first = calls
    _run(fetch_one=fetch)
    assert calls == first


def test_total_failure_serves_last_good_rows_as_stale() -> None:
    async def good(_sym: str) -> dict[str, Any]:
        return _meta(100.0, 100.0)

    async def bad(_sym: str) -> dict[str, Any]:
        raise RuntimeError("yahoo down")

    _run(fetch_one=good, ttl=0.0)
    out = _run(fetch_one=bad, ttl=0.0)
    assert out["stale"] is True
    assert len(out["rows"]) == len(gi.GLOBAL_INDICES)


def test_cold_start_failure_returns_empty_not_error() -> None:
    async def bad(_sym: str) -> dict[str, Any]:
        raise RuntimeError("yahoo down")

    out = _run(fetch_one=bad)
    assert out == {"rows": [], "delayed": True, "stale": True}


def test_one_bad_symbol_is_skipped_others_kept() -> None:
    async def fetch(sym: str) -> dict[str, Any]:
        if sym == "^GSPC":
            return {"regularMarketPrice": 0}
        return _meta(100.0, 100.0)

    out = _run(fetch_one=fetch)
    assert len(out["rows"]) == len(gi.GLOBAL_INDICES) - 1
