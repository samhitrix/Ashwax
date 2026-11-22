"""Seller-desk volatility intelligence engine (options_spec.md §2).

Computes IVR/IVP/vol-premium/term-structure/skew/GEX per underlying and
writes to ``options_vol_intel_history`` on the **storage** cadence the spec
states (every 30 minutes) — not the 30-second *compute* cadence also
mentioned in §2. That faster cadence exists to feed a live in-memory cache
for downstream consumers (strategy/strike selectors) that don't exist yet
in this build; running full Tier-2 chain fetches every 30s with nothing
consuming that freshness would be exactly the rate-limit problem §12 warns
against. Revisit when the strategy_selector sub-project needs faster reads.
Hourly, also snapshots the full fetched chain to
``options_vol_surface_snapshots`` — the only legitimate data source for the
future options backtester (§17): it must never price historical options
using today's IV.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from src.options.chain_fetcher import classify_quality
from src.options.greeks import DEFAULT_RISK_FREE_RATE, days_to_years
from src.options.greeks import greeks as _bs_greeks
from src.options.nfo_chain import index_spot_symbol, iv_percentile
from src.options.volatility_formulas import (
    classify_term_structure,
    dealer_gex,
    iv_rank,
    realized_vol,
    skew_25delta,
    vol_premium,
)

if TYPE_CHECKING:
    from src.brokers.base import BrokerInterface
    from src.db.repositories.options import OptionsRepository
    from src.options.chain_fetcher import ChainFetcher
    from src.options.nfo_chain import ChainStrike, OptionChain

_LOG = logging.getLogger("options.volatility_engine")
_IST = ZoneInfo("Asia/Kolkata")

# DTE buckets for term-structure sampling (options_spec.md §2).
_DTE_BUCKETS = [7, 14, 30, 60, 90]
_TARGET_DELTA = 0.25
_SNAPSHOT_INTERVAL = timedelta(hours=1)
_LOOP_INTERVAL_SECONDS = 1800.0  # 30 min — matches §2's storage cadence, see module docstring


def _nearest_call_iv_at_delta(chain: OptionChain, target: float) -> float | None:
    candidates: list[ChainStrike] = [
        row for row in chain.strikes if row.call_delta is not None and row.call_iv is not None
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs(r.call_delta - target))  # type: ignore[operator]
    return best.call_iv


def _nearest_put_iv_at_delta(chain: OptionChain, target: float) -> float | None:
    # Put delta is negative by convention (src/options/greeks.py); a "25-delta
    # put" means delta closest to -target.
    candidates: list[ChainStrike] = [
        row for row in chain.strikes if row.put_delta is not None and row.put_iv is not None
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: abs(r.put_delta - (-target)))  # type: ignore[operator]
    return best.put_iv


class VolatilityEngine:
    def __init__(
        self,
        broker: BrokerInterface,
        chain_fetcher: ChainFetcher,
        repo: OptionsRepository,
        indices: list[str],
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    ) -> None:
        self._broker = broker
        self._chain_fetcher = chain_fetcher
        self._repo = repo
        self._indices = indices
        self._r = risk_free_rate
        self._last_snapshot: dict[str, datetime] = {}

    async def compute_and_store(self, underlying: str) -> None:
        now = datetime.now(_IST)
        root = underlying.split(":")[-1].upper()

        tier1 = await self._chain_fetcher.fetch_tier1(underlying)
        if tier1 is None:
            _LOG.info("vol_engine_no_chain underlying=%s", root)
            return
        current_iv = tier1.atm_iv()

        tier2 = await self._chain_fetcher.fetch_tier2(underlying, _DTE_BUCKETS)
        iv_by_dte = {
            bucket: atm_iv
            for bucket, chain in tier2.items()
            if (atm_iv := chain.atm_iv()) is not None
        }
        term_structure = classify_term_structure(iv_by_dte)

        skew_chain = tier2.get(30) or tier1
        call_25d_iv = _nearest_call_iv_at_delta(skew_chain, _TARGET_DELTA)
        put_25d_iv = _nearest_put_iv_at_delta(skew_chain, _TARGET_DELTA)
        skew = (
            skew_25delta(call_25d_iv, put_25d_iv)
            if call_25d_iv is not None and put_25d_iv is not None
            else None
        )

        closes = await self._fetch_closes(root)
        rv20 = realized_vol(closes, 20)
        rv30 = realized_vol(closes, 30)
        rv60 = realized_vol(closes, 60)
        premium = (
            vol_premium(current_iv, rv30) if current_iv is not None and rv30 is not None else None
        )

        gex = self._compute_gex(tier2.get(30) or tier1)

        history = await self._repo.recent_vol_intel_ivs(root)
        ivr = iv_rank(current_iv, history) if current_iv is not None else None
        ivp = iv_percentile(current_iv, history) if current_iv is not None else None
        if len(history) >= 252:
            confidence = "FULL"
        elif len(history) >= 90:
            confidence = "PARTIAL"
        else:
            confidence = "VIX_PROXY"

        await self._repo.insert_vol_intel(
            root,
            now,
            iv_30d=current_iv,
            iv_realized_20d=rv20,
            iv_realized_30d=rv30,
            iv_realized_60d=rv60,
            ivr=ivr,
            ivp=ivp,
            vol_premium=premium,
            term_structure=term_structure,
            skew_25delta=skew,
            gex=gex,
            confidence=confidence,
        )

        last_snap = self._last_snapshot.get(root)
        if last_snap is None or (now - last_snap) >= _SNAPSHOT_INTERVAL:
            surface = self._build_surface(list(tier2.values()) or [tier1], now)
            if surface:
                await self._repo.insert_vol_surface_snapshot(root, now, surface)
            self._last_snapshot[root] = now

    async def _fetch_closes(self, root: str) -> list[float]:
        try:
            ohlcv = await self._broker.get_ohlcv(f"NSE:{index_spot_symbol(root)}", "day", 65)
        except Exception as exc:  # noqa: BLE001 — best-effort, RV stays None on failure
            _LOG.warning("vol_engine_ohlcv_failed underlying=%s err=%s", root, exc)
            return []
        if ohlcv is None or ohlcv.empty or "close" not in ohlcv.columns:
            return []
        return [float(c) for c in ohlcv["close"]]

    def _compute_gex(self, chain: OptionChain | None) -> float | None:
        if chain is None:
            return None
        t = days_to_years(chain.days_to_expiry)
        if t <= 0.0:
            return None
        strikes: list[tuple[float, float, float, float]] = []
        for row in chain.strikes:
            call_gamma, call_oi = 0.0, 0
            if row.call is not None and row.call_iv is not None:
                call_gamma = _bs_greeks(chain.spot, row.strike, t, self._r, row.call_iv, "CE")[
                    "gamma"
                ]
                call_oi = row.call.oi
            put_gamma, put_oi = 0.0, 0
            if row.put is not None and row.put_iv is not None:
                put_gamma = _bs_greeks(chain.spot, row.strike, t, self._r, row.put_iv, "PE")[
                    "gamma"
                ]
                put_oi = row.put.oi
            strikes.append((call_gamma, call_oi, put_gamma, put_oi))
        if not strikes:
            return None
        return dealer_gex(strikes, spot=chain.spot, lot_size=chain.lot_size)

    def _build_surface(self, chains: list[OptionChain | None], now: datetime) -> dict[str, Any]:
        surface: dict[str, Any] = {}
        for chain in chains:
            if chain is None:
                continue
            t = days_to_years(chain.days_to_expiry)
            if t <= 0.0:
                continue
            strikes_out: dict[str, Any] = {}
            for row in chain.strikes:
                for kind, quote, iv in (
                    ("CE", row.call, row.call_iv),
                    ("PE", row.put, row.put_iv),
                ):
                    if quote is None or iv is None:
                        continue
                    g = _bs_greeks(chain.spot, row.strike, t, self._r, iv, kind)
                    quality = classify_quality(quote, now=now, last_traded=None)
                    strikes_out[f"{int(row.strike)}{kind}"] = {
                        "iv": iv,
                        "delta": g["delta"],
                        "gamma": g["gamma"],
                        "theta": g["theta"],
                        "vega": g["vega"],
                        "bid": quote.bid,
                        "ask": quote.ask,
                        "oi": quote.oi,
                        "volume": quote.volume,
                        "data_quality": quality.value,
                    }
            surface[chain.expiry.date().isoformat()] = strikes_out
        return surface

    async def run_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            _LOG.info("vol_engine_cycle_start indices=%s", self._indices)
            for underlying in self._indices:
                try:
                    await self.compute_and_store(underlying)
                    _LOG.info("vol_engine_cycle_stored underlying=%s", underlying)
                except Exception as exc:  # noqa: BLE001 — one bad underlying must not kill the loop
                    _LOG.warning("vol_engine_cycle_failed underlying=%s err=%s", underlying, exc)
            await asyncio.sleep(_LOOP_INTERVAL_SECONDS)
