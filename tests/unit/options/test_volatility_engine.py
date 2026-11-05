"""Tests for the seller-desk volatility intelligence engine (options_spec.md §2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from src.brokers.base import OptionQuote
from src.options.nfo_chain import ChainStrike, OptionChain
from src.options.volatility_engine import VolatilityEngine

_IST = ZoneInfo("Asia/Kolkata")


def _quote(mid: float) -> OptionQuote:
    return OptionQuote("SYM", ltp=mid, bid=mid - 0.5, ask=mid + 0.5, oi=1000, volume=500)


def _chain(expiry_days: int, spot: float = 24000.0, iv: float = 0.18) -> OptionChain:
    expiry = datetime.now(_IST) + timedelta(days=expiry_days)
    rows = [
        ChainStrike(
            strike=strike,
            call=_quote(100.0),
            put=_quote(90.0),
            call_iv=iv,
            put_iv=iv + 0.02,
            call_delta=call_delta,
            put_delta=put_delta,
            call_symbol=f"NIFTY{int(strike)}CE",
            put_symbol=f"NIFTY{int(strike)}PE",
        )
        for strike, call_delta, put_delta in [
            (23600.0, 0.75, -0.25),
            (24000.0, 0.50, -0.50),
            (24400.0, 0.25, -0.75),
        ]
    ]
    return OptionChain(
        underlying="NIFTY",
        spot=spot,
        expiry=expiry,
        days_to_expiry=expiry_days,
        lot_size=25,
        strikes=rows,
    )


class _FakeChainFetcher:
    def __init__(self, tier1: OptionChain | None, tier2: dict[int, OptionChain]) -> None:
        self._tier1 = tier1
        self._tier2 = tier2

    async def fetch_tier1(self, underlying: str) -> OptionChain | None:
        return self._tier1

    async def fetch_tier2(self, underlying: str, dte_buckets: list[int]) -> dict[int, OptionChain]:
        return self._tier2


class _FakeBroker:
    def __init__(self, closes: list[float]) -> None:
        self._closes = closes

    async def get_ohlcv(self, symbol: str, interval: str, days: int) -> pd.DataFrame:
        return pd.DataFrame({"close": self._closes})


class _FakeRepo:
    def __init__(self, history: list[float] | None = None) -> None:
        self.inserted: list[tuple] = []
        self.surface_inserted: list[tuple] = []
        self._history = history or []

    async def recent_vol_intel_ivs(self, symbol: str, limit: int = 252) -> list[float]:
        return self._history

    async def insert_vol_intel(self, symbol: str, timestamp: datetime, **kwargs: object) -> None:
        self.inserted.append((symbol, timestamp, kwargs))

    async def insert_vol_surface_snapshot(
        self, symbol: str, timestamp: datetime, surface_data: dict
    ) -> None:
        self.surface_inserted.append((symbol, timestamp, surface_data))


async def test_compute_and_store_no_chain_skips() -> None:
    fetcher = _FakeChainFetcher(tier1=None, tier2={})
    repo = _FakeRepo()
    engine = VolatilityEngine(_FakeBroker([]), fetcher, repo, ["NIFTY"])
    await engine.compute_and_store("NSE:NIFTY")
    assert repo.inserted == []


async def test_compute_and_store_writes_vol_intel() -> None:
    tier1 = _chain(7)
    tier2 = {7: _chain(7), 30: _chain(30, iv=0.15), 90: _chain(90, iv=0.20)}
    closes = [24000.0 * (1.0005**i) for i in range(65)]
    fetcher = _FakeChainFetcher(tier1, tier2)
    repo = _FakeRepo(history=[0.16, 0.17, 0.18, 0.19, 0.20])
    engine = VolatilityEngine(_FakeBroker(closes), fetcher, repo, ["NIFTY"])

    await engine.compute_and_store("NSE:NIFTY")

    assert len(repo.inserted) == 1
    symbol, _ts, kwargs = repo.inserted[0]
    assert symbol == "NIFTY"
    assert kwargs["iv_30d"] is not None
    assert kwargs["term_structure"] in {"contango", "backwardation", "flat"}
    assert kwargs["skew_25delta"] is not None
    assert kwargs["confidence"] == "VIX_PROXY"  # only 5 history samples


async def test_compute_and_store_writes_surface_snapshot_first_cycle() -> None:
    tier1 = _chain(7)
    tier2 = {30: _chain(30)}
    fetcher = _FakeChainFetcher(tier1, tier2)
    repo = _FakeRepo()
    engine = VolatilityEngine(_FakeBroker([24000.0] * 65), fetcher, repo, ["NIFTY"])

    await engine.compute_and_store("NSE:NIFTY")

    assert len(repo.surface_inserted) == 1
    _, _, surface = repo.surface_inserted[0]
    assert isinstance(surface, dict)
    assert len(surface) >= 1


async def test_compute_and_store_skips_surface_snapshot_within_hour() -> None:
    tier1 = _chain(7)
    tier2 = {30: _chain(30)}
    fetcher = _FakeChainFetcher(tier1, tier2)
    repo = _FakeRepo()
    engine = VolatilityEngine(_FakeBroker([24000.0] * 65), fetcher, repo, ["NIFTY"])

    await engine.compute_and_store("NSE:NIFTY")
    await engine.compute_and_store("NSE:NIFTY")

    assert len(repo.surface_inserted) == 1
