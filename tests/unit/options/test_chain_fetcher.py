"""Tests for the seller-desk 3-tier chain fetcher (options_spec.md §12)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.brokers.base import Instrument, OptionQuote
from src.options.chain_fetcher import ChainFetcher, StrikeQuality, classify_quality
from src.options.greeks import bs_price, days_to_years

_IST = ZoneInfo("Asia/Kolkata")
_EXPIRY = datetime.now(_IST) + timedelta(days=20)
_SPOT = 24000.0
_IV = 0.18
_R = 0.065
_STRIKES = [23800.0, 23900.0, 24000.0, 24100.0, 24200.0]


class _FakeRedis:
    """In-memory double matching the get/setex interface used by ChainFetcher."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._data[key] = value


class _FakeBroker:
    async def get_instruments(self, exchange: str) -> list[Instrument]:
        return [
            Instrument(
                symbol=f"NIFTY24JUL{int(k)}{kind}",
                exchange="NFO",
                instrument_token=f"{int(k)}{kind}",
                instrument_type=kind,
                lot_size=25,
                tick_size=0.05,
                expiry=_EXPIRY,
                strike=k,
                name="NIFTY",
            )
            for k in _STRIKES
            for kind in ("CE", "PE")
        ]

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {symbols[0]: _SPOT}

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        t = days_to_years(20)
        out: dict[str, OptionQuote] = {}
        for sym in symbols:
            kind = "CE" if sym.endswith("CE") else "PE"
            strike = float(sym[-len("24000CE") : -2])
            price = bs_price(_SPOT, strike, t, _R, _IV, kind)
            out[sym] = OptionQuote(sym, price, price - 0.5, price + 0.5, oi=5000, volume=2000)
        return out


def test_classify_quality_illiquid_when_no_quote() -> None:
    now = datetime.now(_IST)
    assert classify_quality(None, now=now, last_traded=now) == StrikeQuality.ILLIQUID


def test_classify_quality_illiquid_when_zero_bid() -> None:
    now = datetime.now(_IST)
    q = OptionQuote("X", ltp=10.0, bid=0.0, ask=10.5, oi=100, volume=100)
    assert classify_quality(q, now=now, last_traded=now) == StrikeQuality.ILLIQUID


def test_classify_quality_stale() -> None:
    now = datetime.now(_IST)
    last_traded = now - timedelta(minutes=45)
    q = OptionQuote("X", ltp=10.0, bid=9.9, ask=10.1, oi=100, volume=100)
    assert classify_quality(q, now=now, last_traded=last_traded) == StrikeQuality.STALE


def test_classify_quality_wide_spread() -> None:
    now = datetime.now(_IST)
    # spread = 2.0, mid = 11.0 -> 18% > 10% threshold
    q = OptionQuote("X", ltp=10.0, bid=10.0, ask=12.0, oi=100, volume=100)
    assert classify_quality(q, now=now, last_traded=now) == StrikeQuality.WIDE_SPREAD


def test_classify_quality_fresh() -> None:
    now = datetime.now(_IST)
    q = OptionQuote("X", ltp=10.0, bid=9.9, ask=10.1, oi=100, volume=100)
    assert classify_quality(q, now=now, last_traded=now) == StrikeQuality.FRESH


async def test_fetch_tier1_returns_chain() -> None:
    fetcher = ChainFetcher(_FakeBroker(), _FakeRedis())
    chain = await fetcher.fetch_tier1("NSE:NIFTY")
    assert chain is not None
    assert chain.underlying == "NIFTY"
    assert chain.atm() is not None


async def test_fetch_tier2_populates_cache() -> None:
    redis = _FakeRedis()
    fetcher = ChainFetcher(_FakeBroker(), redis)
    result = await fetcher.fetch_tier2("NSE:NIFTY", dte_buckets=[14])
    assert 14 in result
    chain = result[14]
    cache_key = f"options_chain:NIFTY:{chain.expiry.date().isoformat()}"
    assert cache_key in redis._data


async def test_get_cached_tier2_round_trips() -> None:
    redis = _FakeRedis()
    fetcher = ChainFetcher(_FakeBroker(), redis)
    fetched = await fetcher.fetch_tier2("NSE:NIFTY", dte_buckets=[14])
    chain = fetched[14]
    expiry_iso = chain.expiry.date().isoformat()

    cached = await fetcher.get_cached_tier2("NIFTY", expiry_iso)
    assert cached is not None
    assert cached.underlying == "NIFTY"
    assert cached.spot == chain.spot
    assert len(cached.strikes) == len(chain.strikes)
    assert cached.atm() is not None


async def test_get_cached_tier2_miss_returns_none() -> None:
    redis = _FakeRedis()
    fetcher = ChainFetcher(_FakeBroker(), redis)
    assert await fetcher.get_cached_tier2("NIFTY", "2099-01-01") is None
