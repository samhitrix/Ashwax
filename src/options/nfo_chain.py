"""NFO option-chain provider + IV-percentile helper (Phase 6, Layer 2).

Builds a focused option chain around spot for one underlying using the broker's
canonical ``get_instruments("NFO")`` + ``get_option_quotes`` calls, computes
implied vol and per-strike delta via :mod:`src.options.greeks`, and enforces the
``liquidity_min_oi`` / ``liquidity_min_volume`` gates.

No exchange behaviour is invented: strikes/expiries come from the live NFO
instrument dump, premiums/OI/volume from live quotes, IV is solved from the
observed option mid price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.brokers.base import BrokerInterface, Instrument, OptionQuote
from src.options.greeks import DEFAULT_RISK_FREE_RATE, days_to_years, greeks, implied_vol

_LOG = logging.getLogger("options.nfo_chain")
_IST = ZoneInfo("Asia/Kolkata")

# NFO instrument-dump underlying names ("BANKNIFTY", "FINNIFTY") differ from
# the actual NSE index spot-quote trading symbols ("NIFTY BANK", "NIFTY FIN
# SERVICE") — this only matters for index underlyings; for stocks the root
# name and spot symbol are identical, which is why this gap never surfaced
# before the seller-desk engine started requesting index chains. Confirmed
# against the existing "NSE:NIFTY BANK" usage elsewhere in this codebase
# (src/api/server.py) rather than invented.
_INDEX_SPOT_SYMBOL: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
}


def index_spot_symbol(root: str) -> str:
    """Map an NFO underlying root to its real NSE spot/historical-data symbol.

    Public so every caller that needs a live LTP or OHLCV series for an index
    underlying (chain spot price, ADX/trending-score inputs, realized vol)
    uses the same mapping — not reimplemented per call site.
    """
    return _INDEX_SPOT_SYMBOL.get(root.upper(), root.upper())


# Index options that list on BSE's derivatives segment (Kite exchange "BFO") rather than NFO, with
# the exchange-qualified spot symbol. Verified against Kite 2026-10-16: instruments("BFO") serves
# SENSEX options (lot 20, strike step 100, weekly Thursday expiry) and ltp("BSE:SENSEX") the spot.
_BFO_ROOTS: dict[str, str] = {"SENSEX": "BSE:SENSEX"}


def option_exchange(root: str) -> str:
    """Exchange whose instrument dump lists ``root``'s options: 'BFO' for BSE indices, else 'NFO'."""
    return "BFO" if root.upper() in _BFO_ROOTS else "NFO"


def index_spot_quote_symbol(root: str) -> str:
    """Exchange-qualified spot/LTP symbol for an underlying root ('SENSEX' -> 'BSE:SENSEX',
    'NIFTY' -> 'NSE:NIFTY 50'). Use this instead of prefixing ``NSE:`` onto index_spot_symbol()."""
    return _BFO_ROOTS.get(root.upper()) or f"NSE:{index_spot_symbol(root)}"


# Minimum daily ATM-IV samples before an IV percentile is trustworthy. Below
# this, percentile is undefined (None) and IV-conditional options are blocked.
IV_PERCENTILE_MIN_SAMPLES = 20

# Strike window built around spot (each side). Keeps quote fan-out bounded:
# 2*60+1 strikes * 2 legs = 242 symbols, one Kite quote() call (limit 500).
# Must reach the 0.10 delta wing of a credit spread at the far end of the DTE
# band (45d) even at ~25% IV -- ~54 NIFTY strikes out. At 12 the monthly-only
# indices (BANKNIFTY/FINNIFTY, ~35 DTE) never had their target deltas in the
# chain and every spread failed "no strikes within delta tolerance".
_STRIKE_WINDOW = 60


@dataclass
class ChainStrike:
    """One strike row: paired CE/PE quotes enriched with IV + delta."""

    strike: float
    call: OptionQuote | None = None
    put: OptionQuote | None = None
    call_iv: float | None = None
    put_iv: float | None = None
    call_delta: float | None = None
    put_delta: float | None = None
    call_symbol: str = ""  # "NFO:RELIANCE24JUL2800CE"
    put_symbol: str = ""


@dataclass
class OptionChain:
    """A focused option chain snapshot for one underlying + expiry."""

    underlying: str  # root, e.g. "RELIANCE"
    spot: float
    expiry: datetime
    days_to_expiry: int
    lot_size: int
    strikes: list[ChainStrike]

    def atm(self) -> ChainStrike | None:
        """Strike row nearest to spot."""
        if not self.strikes:
            return None
        return min(self.strikes, key=lambda s: abs(s.strike - self.spot))

    def atm_iv(self) -> float | None:
        """ATM implied vol (mean of CE/PE IV at the nearest strike)."""
        row = self.atm()
        if row is None:
            return None
        ivs = [v for v in (row.call_iv, row.put_iv) if v is not None]
        return sum(ivs) / len(ivs) if ivs else None

    def nearest(self, price: float) -> ChainStrike | None:
        """Strike row nearest to an arbitrary price (e.g. target / SL level)."""
        if not self.strikes:
            return None
        return min(self.strikes, key=lambda s: abs(s.strike - price))


def liquidity_ok(quote: OptionQuote | None, min_oi: int, min_volume: int) -> bool:
    """True iff the quote meets the configured OI + volume liquidity floors."""
    if quote is None:
        return False
    return quote.oi >= min_oi and quote.volume >= min_volume


def iv_percentile(
    current_iv: float | None,
    history: list[float],
    *,
    min_samples: int = IV_PERCENTILE_MIN_SAMPLES,
) -> float | None:
    """Percentile (0-100) of ``current_iv`` within ``history``.

    Returns ``None`` (fail-safe: caller blocks IV-conditional options) when
    current_iv is missing or fewer than ``min_samples`` historical samples exist.
    """
    if current_iv is None or len(history) < min_samples:
        return None
    below = sum(1 for v in history if v < current_iv)
    return 100.0 * below / len(history)


class NFOChainProvider:
    """Builds option chains for underlyings from the live NFO instrument dump.

    Instruments are cached for the session (the NFO dump is large and static
    intraday); quotes are always fetched fresh.
    """

    def __init__(
        self, broker: BrokerInterface, risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    ) -> None:
        self._broker = broker
        self._r = risk_free_rate
        self._instruments: dict[str, list[Instrument]] = {}

    async def _load_instruments(self, exchange: str = "NFO") -> list[Instrument]:
        if exchange not in self._instruments:
            self._instruments[exchange] = await self._broker.get_instruments(exchange)
        return self._instruments[exchange]

    @staticmethod
    def _root(underlying: str) -> str:
        """Strip an exchange prefix: 'NSE:RELIANCE' -> 'RELIANCE'."""
        return underlying.split(":")[-1].upper()

    def _select_expiry(
        self, options: list[Instrument], min_days: int, now: datetime
    ) -> datetime | None:
        """Nearest expiry at least ``min_days`` calendar days out."""
        expiries = sorted({i.expiry for i in options if i.expiry is not None})
        for exp in expiries:
            exp_aware = exp if exp.tzinfo else exp.replace(tzinfo=_IST)
            if (exp_aware - now).days >= min_days:
                return exp
        return None

    async def get_chain(
        self,
        underlying: str,
        *,
        min_days_to_expiry: int,
        spot: float | None = None,
        now: datetime | None = None,
        expiry_date: date | None = None,
    ) -> OptionChain | None:
        """Build a focused chain around spot for the nearest qualifying expiry.

        ``expiry_date`` pins one exact expiry instead (an open position's own expiry);
        ``min_days_to_expiry`` is then ignored.

        Returns ``None`` when the underlying has no NFO options or no expiry
        satisfies ``min_days_to_expiry`` (or ``expiry_date`` does not exist).
        """
        root = self._root(underlying)
        now = now or datetime.now(_IST)
        exchange = option_exchange(root)
        instruments = await self._load_instruments(exchange)
        options = [
            i
            for i in instruments
            if i.instrument_type in ("CE", "PE")
            and i.strike is not None
            and (
                i.name.upper() == root
                # BFO lists SENSEX50/BANKEX next to SENSEX; a prefix match would mix their strikes in.
                or (exchange == "NFO" and i.symbol.upper().startswith(root))
            )
        ]
        if not options:
            _LOG.info("no_nfo_options underlying=%s", root)
            return None

        if expiry_date is not None:
            expiry = next(
                (
                    e
                    for e in sorted({i.expiry for i in options if i.expiry is not None})
                    if (e.date() if isinstance(e, datetime) else e) == expiry_date
                ),
                None,
            )
        else:
            expiry = self._select_expiry(options, min_days_to_expiry, now)
        if expiry is None:
            _LOG.info("no_expiry_min_days underlying=%s min_days=%d", root, min_days_to_expiry)
            return None
        options = [i for i in options if i.expiry == expiry]

        if spot is None:
            ltp_map = await self._broker.get_ltp([index_spot_quote_symbol(root)])
            spot = next(iter(ltp_map.values()), 0.0) if ltp_map else 0.0
        if spot <= 0.0:
            _LOG.warning("no_spot underlying=%s", root)
            return None

        # Build the strike window centred on spot.
        all_strikes = sorted({float(i.strike) for i in options if i.strike is not None})
        atm_idx = min(range(len(all_strikes)), key=lambda k: abs(all_strikes[k] - spot))
        lo = max(0, atm_idx - _STRIKE_WINDOW)
        hi = min(len(all_strikes), atm_idx + _STRIKE_WINDOW + 1)
        window = all_strikes[lo:hi]

        by_strike: dict[float, dict[str, Instrument]] = {}
        for inst in options:
            if inst.strike is None or float(inst.strike) not in window:
                continue
            by_strike.setdefault(float(inst.strike), {})[inst.instrument_type] = inst

        symbols: list[str] = []
        for legs in by_strike.values():
            for inst in legs.values():
                symbols.append(f"{exchange}:{inst.symbol}")
        quotes = await self._broker.get_option_quotes(symbols)

        expiry_aware = expiry if expiry.tzinfo else expiry.replace(tzinfo=_IST)
        dte = max((expiry_aware - now).days, 0)
        # Pricing time is REAL time to the 15:30 IST expiry close, not whole days: Kite gives
        # expiry as a bare date (stamped 00:00), so on the eve of expiry `dte` is 0 while ~29h
        # of value remain. t=0 made implied_vol() return None for every strike -> no IV, no
        # delta, ivr=None, regime UNDEFINED (2026-09-28, all three indices expiring 29 Sep).
        expiry_close = (
            expiry_aware
            if (expiry_aware.hour or expiry_aware.minute)
            else expiry_aware.replace(hour=15, minute=30)
        )
        t = days_to_years(max((expiry_close - now).total_seconds(), 0.0) / 86400.0)
        lot_size = next((i.lot_size for i in options), 1)

        rows: list[ChainStrike] = []
        for strike in window:
            legs = by_strike.get(strike, {})
            row = ChainStrike(strike=strike)
            for kind, attr in (("CE", "call"), ("PE", "put")):
                leg_inst = legs.get(kind)
                if leg_inst is None:
                    continue
                sym = f"{exchange}:{leg_inst.symbol}"
                q = quotes.get(sym)
                setattr(row, attr, q)
                setattr(row, f"{attr}_symbol", sym)
                if q is not None and q.mid > 0.0 and t > 0.0:
                    iv = implied_vol(q.mid, spot, strike, t, self._r, kind)
                    setattr(row, f"{attr}_iv", iv)
                    if iv is not None:
                        delta = greeks(spot, strike, t, self._r, iv, kind)["delta"]
                        setattr(row, f"{attr}_delta", delta)
            rows.append(row)

        return OptionChain(
            underlying=root,
            spot=spot,
            expiry=expiry,
            days_to_expiry=dte,
            lot_size=lot_size,
            strikes=rows,
        )
