"""3-tier NFO chain fetcher for the seller-first engine (options_spec.md §12).

Tier 1 (ATM±5, nearest expiry, 30s cadence) is never cached — always a fresh
fetch, for active-position monitoring. Tier 2 (per-DTE-bucket chain, 5min
cadence) is Redis-cached; Tier 3 (deep OTM) is served from the Tier 2 cache
with no separate fetch, so it isn't a distinct method here.

Both tiers reuse ``NFOChainProvider`` (src/options/nfo_chain.py) rather than
re-fetching instruments/quotes with new logic — it already does the
IV/delta computation this engine needs. NFOChainProvider windows each chain
to ATM±12 strikes, not the full unbounded chain the spec describes; that's
wide enough for term-structure/skew/GEX at this build stage. Widening for
deep-OTM wing selection (Iron Condor strikes) is deferred to the
strike_selector sub-project, which needs it and doesn't exist yet.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from src.options.nfo_chain import ChainStrike, NFOChainProvider, OptionChain

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface, OptionQuote

_STALE_MINUTES = 30
_WIDE_SPREAD_PCT = 0.10
_TIER2_CACHE_TTL_SECONDS = 300


class StrikeQuality(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    ILLIQUID = "illiquid"
    WIDE_SPREAD = "wide_spread"


def classify_quality(
    quote: OptionQuote | None,
    *,
    now: datetime,
    last_traded: datetime | None,
) -> StrikeQuality:
    """Per-strike data quality classification (options_spec.md §12).

    ``last_traded`` is optional because the current ``OptionQuote`` model
    (src/brokers/base.py) doesn't carry a last-trade timestamp — when it's
    ``None``, staleness can't be assessed and that check is skipped rather
    than guessed.
    """
    if quote is None or quote.bid == 0.0 or quote.ask == 0.0:
        return StrikeQuality.ILLIQUID
    if last_traded is not None and (now - last_traded) > timedelta(minutes=_STALE_MINUTES):
        return StrikeQuality.STALE
    mid = quote.mid
    if mid > 0.0 and (quote.ask - quote.bid) / mid > _WIDE_SPREAD_PCT:
        return StrikeQuality.WIDE_SPREAD
    return StrikeQuality.FRESH


def _serialize_chain(chain: OptionChain) -> str:
    return json.dumps(
        {
            "underlying": chain.underlying,
            "spot": chain.spot,
            "expiry": chain.expiry.isoformat(),
            "days_to_expiry": chain.days_to_expiry,
            "lot_size": chain.lot_size,
            "strikes": [asdict(row) for row in chain.strikes],
        }
    )


def _deserialize_chain(raw: str) -> OptionChain:
    from src.brokers.base import OptionQuote

    data = json.loads(raw)

    def _quote(d: dict[str, Any] | None) -> OptionQuote | None:
        return None if d is None else OptionQuote(**d)

    strikes = [
        ChainStrike(
            strike=row["strike"],
            call=_quote(row["call"]),
            put=_quote(row["put"]),
            call_iv=row["call_iv"],
            put_iv=row["put_iv"],
            call_delta=row["call_delta"],
            put_delta=row["put_delta"],
            call_symbol=row["call_symbol"],
            put_symbol=row["put_symbol"],
        )
        for row in data["strikes"]
    ]
    return OptionChain(
        underlying=data["underlying"],
        spot=data["spot"],
        expiry=datetime.fromisoformat(data["expiry"]),
        days_to_expiry=data["days_to_expiry"],
        lot_size=data["lot_size"],
        strikes=strikes,
    )


class ChainFetcher:
    def __init__(self, broker: BrokerInterface, redis: Any) -> None:
        self._provider = NFOChainProvider(broker)
        self._redis = redis

    async def fetch_tier1(self, underlying: str) -> OptionChain | None:
        """Nearest expiry, strike window per nfo_chain._STRIKE_WINDOW — always a
        fresh fetch, never cached."""
        return await self._provider.get_chain(underlying, min_days_to_expiry=0)

    async def fetch_for_expiry(self, underlying: str, expiry_date: date) -> OptionChain | None:
        """Fresh chain for one exact expiry (an open position's own), never cached."""
        return await self._provider.get_chain(
            underlying, min_days_to_expiry=0, expiry_date=expiry_date
        )

    async def fetch_tier2(self, underlying: str, dte_buckets: list[int]) -> dict[int, OptionChain]:
        """One chain per DTE bucket (nearest expiry >= each target), Redis-cached."""
        root = underlying.split(":")[-1].upper()
        result: dict[int, OptionChain] = {}
        for bucket in dte_buckets:
            chain = await self._provider.get_chain(underlying, min_days_to_expiry=bucket)
            if chain is None:
                continue
            result[bucket] = chain
            cache_key = f"options_chain:{root}:{chain.expiry.date().isoformat()}"
            await self._redis.setex(cache_key, _TIER2_CACHE_TTL_SECONDS, _serialize_chain(chain))
        return result

    async def get_cached_tier2(self, underlying: str, expiry_iso: str) -> OptionChain | None:
        root = underlying.split(":")[-1].upper()
        raw = await self._redis.get(f"options_chain:{root}:{expiry_iso}")
        if raw is None:
            return None
        return _deserialize_chain(raw)
