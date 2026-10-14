"""Tests for src/options/nfo_chain.py — chain build, liquidity, IV percentile."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from src.brokers.base import Instrument, OptionQuote
from src.options.greeks import bs_price, days_to_years
from src.options.nfo_chain import (
    NFOChainProvider,
    iv_percentile,
    liquidity_ok,
)

_IST = ZoneInfo("Asia/Kolkata")
_NOW = datetime(2026, 6, 14, 10, 0, tzinfo=_IST)
_EXPIRY = _NOW + timedelta(days=30)
_SPOT = 2750.0
_IV = 0.22
_R = 0.065
_STRIKES = [2600.0, 2650.0, 2700.0, 2750.0, 2800.0, 2850.0, 2900.0]


class _FakeBroker:
    """Minimal duck-typed broker exposing only what NFOChainProvider calls."""

    def __init__(self, *, liquid: bool = True, strikes: list[float] | None = None) -> None:
        self._liquid = liquid
        self._strikes = strikes or _STRIKES

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        out: list[Instrument] = []
        for strike in self._strikes:
            for kind in ("CE", "PE"):
                out.append(
                    Instrument(
                        symbol=f"RELIANCE24JUL{int(strike)}{kind}",
                        exchange="NFO",
                        instrument_token=f"{int(strike)}{kind}",
                        instrument_type=kind,
                        lot_size=250,
                        tick_size=0.05,
                        expiry=_EXPIRY,
                        strike=strike,
                        name="RELIANCE",
                    )
                )
        # A different underlying that must be filtered out.
        out.append(
            Instrument(
                symbol="INFY24JUL1500CE",
                exchange="NFO",
                instrument_token="INFY1500CE",
                instrument_type="CE",
                lot_size=400,
                tick_size=0.05,
                expiry=_EXPIRY,
                strike=1500.0,
                name="INFY",
            )
        )
        return out

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        return {symbols[0]: _SPOT}

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        t = days_to_years(30)
        oi = 2000 if self._liquid else 100
        vol = 1000 if self._liquid else 50
        out: dict[str, OptionQuote] = {}
        for sym in symbols:
            # sym like "NFO:RELIANCE24JUL2750CE"
            kind = "CE" if sym.endswith("CE") else "PE"
            strike = float(sym[-len("2750CE") : -2])
            price = bs_price(_SPOT, strike, t, _R, _IV, kind)
            out[sym] = OptionQuote(
                symbol=sym, ltp=price, bid=price - 0.5, ask=price + 0.5, oi=oi, volume=vol
            )
        return out


# ── liquidity_ok ────────────────────────────────────────────────────────────


def test_liquidity_ok_pass() -> None:
    q = OptionQuote(symbol="X", ltp=10, bid=9, ask=11, oi=2000, volume=600)
    assert liquidity_ok(q, min_oi=1000, min_volume=500) is True


def test_liquidity_ok_fails_low_oi() -> None:
    q = OptionQuote(symbol="X", ltp=10, bid=9, ask=11, oi=500, volume=600)
    assert liquidity_ok(q, min_oi=1000, min_volume=500) is False


def test_liquidity_ok_none_quote() -> None:
    assert liquidity_ok(None, min_oi=1000, min_volume=500) is False


# ── iv_percentile (bootstrap) ───────────────────────────────────────────────


def test_iv_percentile_insufficient_history_returns_none() -> None:
    assert iv_percentile(0.22, [0.20] * 5) is None


def test_iv_percentile_none_current_returns_none() -> None:
    assert iv_percentile(None, [0.20] * 30) is None


def test_iv_percentile_computes_rank() -> None:
    history = [0.10 + 0.01 * i for i in range(20)]  # 0.10 .. 0.29
    pct = iv_percentile(0.25, history)
    assert pct is not None
    assert 70.0 <= pct <= 80.0  # 0.25 sits high in the range


# ── get_chain ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_chain_builds_and_filters_underlying() -> None:
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    assert chain.underlying == "RELIANCE"
    assert chain.spot == _SPOT
    assert chain.days_to_expiry == 30
    assert chain.lot_size == 250
    # INFY strike (1500) must not appear.
    assert all(s.strike in _STRIKES for s in chain.strikes)


@pytest.mark.asyncio
async def test_get_chain_window_reaches_far_otm_wings() -> None:
    # Regression: a +/-12 window never contained the 0.10-delta wing of a
    # ~35-DTE credit spread on BANKNIFTY/FINNIFTY, so every spread failed
    # "no strikes within delta tolerance". A 40-strike-out wing must be present.
    wide = [float(s) for s in range(2000, 3500, 10)]  # ATM 2750 -> 75 strikes each side
    provider = NFOChainProvider(_FakeBroker(strikes=wide), risk_free_rate=_R)
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    strikes = [s.strike for s in chain.strikes]
    assert 2750.0 - 400.0 in strikes and 2750.0 + 400.0 in strikes
    assert 2750.0 - 700.0 not in strikes  # still bounded (60 strikes out = 600)


@pytest.mark.asyncio
async def test_get_chain_atm_and_iv() -> None:
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    atm = chain.atm()
    assert atm is not None
    assert atm.strike == 2750.0  # nearest to spot
    atm_iv = chain.atm_iv()
    assert atm_iv is not None
    assert abs(atm_iv - _IV) < 0.01  # IV round-trips from generated premiums


@pytest.mark.asyncio
async def test_get_chain_no_expiry_satisfies_min_days() -> None:
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    # Expiry is 30 days out; demand 45 → None.
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=45, now=_NOW)
    assert chain is None


class _ExpiryEveBroker(_FakeBroker):
    """Kite hands back expiry as a bare date, stamped 00:00 -- the eve of expiry is then
    < 1 day away and `(expiry - now).days` truncates to 0."""

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        eve = datetime(2026, 6, 15)  # naive midnight, exactly as the Kite adapter builds it
        return [
            Instrument(
                symbol=f"RELIANCE24JUN{int(k)}{kind}",
                exchange="NFO",
                instrument_token=f"{int(k)}{kind}",
                instrument_type=kind,
                lot_size=250,
                tick_size=0.05,
                expiry=eve,
                strike=k,
                name="RELIANCE",
            )
            for k in _STRIKES
            for kind in ("CE", "PE")
        ]

    async def get_option_quotes(self, symbols: list[str]) -> dict[str, OptionQuote]:
        # Real time to the 15:30 IST close on 15 Jun, from _NOW (10:00 on 14 Jun).
        t = (datetime(2026, 6, 15, 15, 30, tzinfo=_IST) - _NOW).total_seconds() / 86400 / 365
        out: dict[str, OptionQuote] = {}
        for sym in symbols:
            kind = "CE" if sym.endswith("CE") else "PE"
            strike = float(sym[-len("2750CE") : -2])
            price = bs_price(_SPOT, strike, t, _R, _IV, kind)
            out[sym] = OptionQuote(
                symbol=sym, ltp=price, bid=price - 0.5, ask=price + 0.5, oi=2000, volume=1000
            )
        return out


@pytest.mark.asyncio
async def test_get_chain_still_has_iv_and_delta_on_the_eve_of_expiry() -> None:
    # Regression (2026-09-28): on the calendar day before expiry dte truncated to 0, t was
    # 0, implied_vol() returned None for EVERY strike -> ivr=None, regime UNDEFINED, and
    # no leg delta for any position in the nearest expiry. There is still ~29 hours of
    # real time value; the chain must use it.
    provider = NFOChainProvider(_ExpiryEveBroker(), risk_free_rate=_R)
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=0, now=_NOW)
    assert chain is not None
    atm = chain.atm()
    assert atm is not None and atm.call_iv is not None and atm.put_iv is not None
    assert abs(chain.atm_iv() - _IV) < 0.02  # type: ignore[operator]
    assert atm.call_delta is not None and 0.4 < atm.call_delta < 0.6


@pytest.mark.asyncio
async def test_get_chain_on_expiry_day_prices_until_the_close_then_stops() -> None:
    provider = NFOChainProvider(_ExpiryEveBroker(), risk_free_rate=_R)
    expiry_day = datetime(2026, 6, 15).date()
    morning = datetime(2026, 6, 15, 10, 0, tzinfo=_IST)
    chain = await provider.get_chain(
        "NSE:RELIANCE", min_days_to_expiry=0, now=morning, expiry_date=expiry_day
    )
    assert chain is not None and chain.atm_iv() is not None  # 5.5h of value left
    after_close = datetime(2026, 6, 15, 15, 45, tzinfo=_IST)
    chain = await provider.get_chain(
        "NSE:RELIANCE", min_days_to_expiry=0, now=after_close, expiry_date=expiry_day
    )
    assert chain is not None and chain.atm_iv() is None  # nothing left to price


@pytest.mark.asyncio
async def test_get_chain_by_exact_expiry_date() -> None:
    # An open position needs ITS expiry's chain, not the nearest one (2026-09-28).
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    chain = await provider.get_chain(
        "NSE:RELIANCE", min_days_to_expiry=0, now=_NOW, expiry_date=_EXPIRY.date()
    )
    assert chain is not None and chain.expiry.date() == _EXPIRY.date()


@pytest.mark.asyncio
async def test_get_chain_by_expiry_date_returns_none_when_no_such_expiry() -> None:
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    other = (_EXPIRY + timedelta(days=7)).date()
    chain = await provider.get_chain(
        "NSE:RELIANCE", min_days_to_expiry=0, now=_NOW, expiry_date=other
    )
    assert chain is None


@pytest.mark.asyncio
async def test_get_chain_unknown_underlying_returns_none() -> None:
    provider = NFOChainProvider(_FakeBroker(), risk_free_rate=_R)
    chain = await provider.get_chain("NSE:NOTLISTED", min_days_to_expiry=14, now=_NOW)
    assert chain is None


# ── index spot-symbol mapping (bug: NFO underlying name != NSE spot symbol) ──


class _SymbolCapturingBroker(_FakeBroker):
    """Records which LTP symbol was actually requested."""

    def __init__(self, underlying_root: str) -> None:
        super().__init__()
        self._root = underlying_root
        self.requested_ltp_symbols: list[str] = []

    async def get_instruments(self, exchange: str) -> list[Instrument]:
        return [
            Instrument(
                symbol=f"{self._root}24JUL{int(strike)}{kind}",
                exchange="NFO",
                instrument_token=f"{int(strike)}{kind}",
                instrument_type=kind,
                lot_size=25,
                tick_size=0.05,
                expiry=_EXPIRY,
                strike=strike,
                name=self._root,
            )
            for strike in _STRIKES
            for kind in ("CE", "PE")
        ]

    async def get_ltp(self, symbols: list[str]) -> dict[str, float]:
        self.requested_ltp_symbols.extend(symbols)
        return {symbols[0]: _SPOT}


@pytest.mark.asyncio
async def test_get_chain_banknifty_uses_correct_spot_symbol() -> None:
    broker = _SymbolCapturingBroker("BANKNIFTY")
    provider = NFOChainProvider(broker, risk_free_rate=_R)
    chain = await provider.get_chain("NSE:BANKNIFTY", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    assert broker.requested_ltp_symbols == ["NSE:NIFTY BANK"]


@pytest.mark.asyncio
async def test_get_chain_finnifty_uses_correct_spot_symbol() -> None:
    broker = _SymbolCapturingBroker("FINNIFTY")
    provider = NFOChainProvider(broker, risk_free_rate=_R)
    chain = await provider.get_chain("NSE:FINNIFTY", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    assert broker.requested_ltp_symbols == ["NSE:NIFTY FIN SERVICE"]


@pytest.mark.asyncio
async def test_get_chain_nifty_uses_correct_spot_symbol() -> None:
    broker = _SymbolCapturingBroker("NIFTY")
    provider = NFOChainProvider(broker, risk_free_rate=_R)
    chain = await provider.get_chain("NSE:NIFTY", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    assert broker.requested_ltp_symbols == ["NSE:NIFTY 50"]


@pytest.mark.asyncio
async def test_get_chain_stock_underlying_unaffected_by_index_mapping() -> None:
    broker = _SymbolCapturingBroker("RELIANCE")
    provider = NFOChainProvider(broker, risk_free_rate=_R)
    chain = await provider.get_chain("NSE:RELIANCE", min_days_to_expiry=14, now=_NOW)
    assert chain is not None
    assert broker.requested_ltp_symbols == ["NSE:RELIANCE"]


def test_index_spot_symbol_known_indices() -> None:
    from src.options.nfo_chain import index_spot_symbol

    assert index_spot_symbol("NIFTY") == "NIFTY 50"
    assert index_spot_symbol("BANKNIFTY") == "NIFTY BANK"
    assert index_spot_symbol("FINNIFTY") == "NIFTY FIN SERVICE"


def test_index_spot_symbol_passthrough_for_stocks() -> None:
    from src.options.nfo_chain import index_spot_symbol

    assert index_spot_symbol("RELIANCE") == "RELIANCE"


def test_index_spot_symbol_case_insensitive() -> None:
    from src.options.nfo_chain import index_spot_symbol

    assert index_spot_symbol("banknifty") == "NIFTY BANK"
