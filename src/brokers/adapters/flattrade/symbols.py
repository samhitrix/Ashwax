"""
Flattrade symbol mapping (canonical symbol <-> `tsym` / exchange token).

Facts (docs/superpowers/specs/2026-10-15-phase0-broker-api-findings.md, A5 / C-5):
  - Scrip masters are public CSVs under SCRIP_MASTER_BASE_URL with the header
    `Exchange,Token,Lotsize,Symbol,Tradingsymbol,Instrument,Expiry,Strike,Optiontype`.
  - NSE equity tradingsymbol is `<SYMBOL>-EQ` (series BE is `<SYMBOL>-BE`).
  - Index spots are `Instrument == "INDEX"` rows whose tradingsymbol is a display
    name: `Nifty 50` (26000), `Nifty Bank` (26009), `INDIAVIX` (26017).
  - NFO tradingsymbols (`NIFTY29DEC26P21050`, `NIFTY27OCT26F`) are used as given;
    Expiry is `DD-MON-YYYY`, Strike is in rupees (`-0.01` on futures), lot size is
    `Lotsize`.
  - There is NO tick-size column: `Instrument.tick_size` is DEFAULT_TICK_SIZE, a
    placeholder. The true tick comes from `GetQuotes.ti` (task F3 rounds with it).

Mapping rules (neutral names the codebase passes -> Flattrade rows), per exchange:
  1. every raw tradingsymbol is a key (`RELIANCE-EQ`, `Nifty 50`, `NIFTY29DEC26P21050`);
  2. NSE `-EQ` rows also answer to the bare name (`RELIANCE`) — never `-BE`;
  3. NSE index rows also answer to the upper-cased tradingsymbol (`NIFTY 50`), the
     underlying `Symbol` column (`NIFTY`, `BANKNIFTY`) and their space-free forms
     (`INDIAVIX` for `INDIA VIX`), plus the explicit codebase names in
     `_INDEX_NAME_ALIASES` keyed by token (`NIFTY FIN SERVICE` -> 26037 `Nifty Fin`).
A malformed row (e.g. a non-numeric Lotsize) is skipped and counted; a changed
header fails the whole load softly.
Aliases are added with setdefault after all raw keys, so an alias never replaces a
raw key or an equity alias (a clash is logged). Lookup is exact, then upper-case,
then upper-case without spaces. An `EXCH:` prefix selects the exchange.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from src.brokers.base import Instrument

_log = logging.getLogger(__name__)  # nests under "src.brokers" -> brokers.log

SCRIP_MASTER_BASE_URL = "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/"

# Exchanges this adapter can resolve, and the files that make up each one. A
# multi-file exchange loads all-or-nothing so a half-loaded universe never looks
# complete.
SCRIP_MASTER_FILES: dict[str, tuple[str, ...]] = {
    "NSE": ("NSE_Equity.csv",),
    "NFO": ("Nfo_Index_Derivatives.csv", "Nfo_Equity_Derivatives.csv"),
}

# Placeholder only — the scrip master has no tick column (findings A5). Correct for
# NFO options; NSE equities can have a smaller tick. Use GetQuotes `ti` for rounding.
DEFAULT_TICK_SIZE = 0.05
DOWNLOAD_TIMEOUT_SECONDS = 60.0

_COLUMNS = (
    "Exchange",
    "Token",
    "Lotsize",
    "Symbol",
    "Tradingsymbol",
    "Instrument",
    "Expiry",
    "Strike",
    "Optiontype",
)

FetchText = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class ScripRow:
    """One scrip-master row, fields kept as the file's strings."""

    exchange: str
    token: str
    lot_size: str
    symbol: str  # underlying, e.g. NIFTY
    tradingsymbol: str  # what the API takes as `tsym`
    instrument: str  # EQ / BE / INDEX / OPTIDX / FUTIDX / OPTSTK / FUTSTK ...
    expiry: str
    strike: str
    option_type: str


class Resolved(NamedTuple):
    token: str
    exchange: str
    tradingsymbol: str


def parse_scrip_master(text: str) -> list[ScripRow]:
    """Parse one scrip-master CSV.

    Raises:
        ValueError: the header is not the documented one (format change).
    """
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = [h.strip() for h in next(reader, [])]
    if tuple(header[: len(_COLUMNS)]) != _COLUMNS:
        raise ValueError(f"unexpected Flattrade scrip-master header: {header[:12]}")
    rows: list[ScripRow] = []
    for cells in reader:
        if not cells or not any(c.strip() for c in cells):
            continue
        vals = [c.strip() for c in cells] + [""] * (len(_COLUMNS) - len(cells))
        rows.append(ScripRow(*vals[: len(_COLUMNS)]))
    return rows


def _instrument_type(row: ScripRow) -> str:
    kind = row.instrument.upper()
    if kind.startswith("OPT") and row.option_type.upper() in ("CE", "PE"):
        return row.option_type.upper()
    if kind.startswith("FUT"):
        return "FUT"
    return kind  # EQ, BE, INDEX, or an unmapped type kept verbatim


def to_instrument(row: ScripRow) -> Instrument:
    """Translate a row to Instrument. `tick_size` is DEFAULT_TICK_SIZE (see module doc).

    Strike is set for option rows only (rupees; a negative value means none).
    """
    expiry: datetime | None = None
    if row.expiry:
        try:
            expiry = datetime.strptime(row.expiry, "%d-%b-%Y")
        except ValueError:
            expiry = None
    kind = _instrument_type(row)
    strike: float | None = None
    if kind in ("CE", "PE"):
        try:
            value = float(row.strike)
        except ValueError:
            value = -1.0
        strike = value if value >= 0 else None
    return Instrument(
        symbol=row.tradingsymbol,
        exchange=row.exchange,
        instrument_token=row.token,
        instrument_type=kind,
        lot_size=int(float(row.lot_size or 1)),
        tick_size=DEFAULT_TICK_SIZE,
        expiry=expiry,
        strike=strike,
        name=row.symbol,
    )


def _squash(key: str) -> str:
    return key.upper().replace(" ", "")


# Index spot names this codebase passes (Kite-style: nfo_chain.index_spot_symbol,
# the Angel alias list) that the generic aliases cannot derive, keyed by the
# Flattrade INDEX-row token from the findings A5 real rows. Applied only when that
# token exists as an NSE INDEX row; no token here is guessed.
_INDEX_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "26000": ("NIFTY 50",),  # Nifty 50
    "26009": ("NIFTY BANK",),  # Nifty Bank
    "26017": ("INDIA VIX",),  # INDIAVIX
    "26037": ("NIFTY FIN SERVICE",),  # Nifty Fin (FINNIFTY)
}


def build_token_table(rows: Iterable[ScripRow], exchange: str) -> dict[str, tuple[str, str]]:
    """{key: (token, exchange)} for one exchange, raw tradingsymbols plus aliases."""
    rows = [r for r in rows if r.exchange == exchange and r.token and r.tradingsymbol]
    table: dict[str, tuple[str, str]] = {r.tradingsymbol: (r.token, exchange) for r in rows}
    if exchange != "NSE":
        return table
    for r in rows:
        if r.tradingsymbol.endswith("-EQ"):
            table.setdefault(r.tradingsymbol[: -len("-EQ")], (r.token, exchange))
    skipped: set[str] = set()
    for r in rows:
        if r.instrument.upper() != "INDEX":
            continue
        entry = (r.token, exchange)
        keys = {r.tradingsymbol.upper(), _squash(r.tradingsymbol)}
        if r.symbol:
            keys |= {r.symbol.upper(), _squash(r.symbol)}
        for name in _INDEX_NAME_ALIASES.get(r.token, ()):
            keys |= {name.upper(), _squash(name)}
        for key in keys:
            if table.setdefault(key, entry) != entry:
                skipped.add(key)
    if skipped:
        _log.warning(
            "flattrade_index_alias_skipped keys=%s reason=key_already_taken", sorted(skipped)
        )
    return table


def lookup(table: dict[str, tuple[str, str]], bare: str) -> tuple[str, str] | None:
    """Exact, then upper-case, then upper-case without spaces."""
    return table.get(bare) or table.get(bare.upper()) or table.get(_squash(bare))


def _build_exchange(
    texts: list[str], exchange: str
) -> tuple[dict[str, tuple[str, str]], dict[str, str], list[Instrument], int]:
    """Parse the files of one exchange -> (token table, {token: tsym}, instruments,
    number of malformed rows skipped). A bad header raises ValueError (whole load
    fails soft); a single malformed row (e.g. non-numeric Lotsize) is skipped so it
    can neither resolve nor break the load."""
    good: list[ScripRow] = []
    instruments: dict[str, Instrument] = {}
    skipped = 0
    for text in texts:
        for r in parse_scrip_master(text):
            if r.exchange != exchange or not r.token or not r.tradingsymbol:
                continue
            try:
                inst = to_instrument(r)
            except ValueError:
                skipped += 1
                continue
            good.append(r)
            instruments.setdefault(r.token, inst)
    tsyms = {tok: inst.symbol for tok, inst in instruments.items()}  # symbol == tsym here
    return build_token_table(good, exchange), tsyms, list(instruments.values()), skipped


async def default_fetch_text(url: str) -> str:
    """Download one scrip-master file with aiohttp (60 s timeout); raises on failure."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as http, http.get(url) as resp:
        resp.raise_for_status()
        text: str = await resp.text()
        return text


class FlattradeSymbols:
    """Per-exchange scrip-master cache: token table, tradingsymbols and Instruments.

    Loading never raises (fail-soft, logged, retried on the next call); a symbol
    that cannot be resolved raises KeyError (fail-loud) — the same pattern as the
    Angel One adapter.
    """

    def __init__(self, fetch_text: FetchText | None = None) -> None:
        self._fetch: FetchText = fetch_text or default_fetch_text
        self._tables: dict[str, dict[str, tuple[str, str]]] = {}
        self._tsym: dict[str, dict[str, str]] = {}  # exchange -> {token: tradingsymbol}
        self._instruments: dict[str, list[Instrument]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def is_supported(self, exchange: str) -> bool:
        return exchange in SCRIP_MASTER_FILES

    async def load(self, exchange: str = "NSE") -> int:
        """Load one exchange's scrip master; returns the number of keys cached (0 on failure)."""
        if self._tables.get(exchange):
            return len(self._tables[exchange])
        files = SCRIP_MASTER_FILES.get(exchange)
        if files is None:
            _log.warning("flattrade_scrip_master_unsupported exchange=%s", exchange)
            return 0
        lock = self._locks.setdefault(exchange, asyncio.Lock())
        async with lock:
            if self._tables.get(exchange):
                return len(self._tables[exchange])
            try:
                texts = [await self._fetch(SCRIP_MASTER_BASE_URL + name) for name in files]
                # CSV parsing of the large derivative files is CPU work: off the event loop.
                built = await asyncio.to_thread(_build_exchange, texts, exchange)
            except Exception as exc:  # noqa: BLE001 — fail soft; resolve() fails loud later
                _log.warning("flattrade_scrip_master_load_failed exchange=%s err=%s", exchange, exc)
                return 0
            table, tsyms, instruments, skipped = built
            if skipped:
                _log.warning(
                    "flattrade_scrip_rows_skipped exchange=%s skipped=%d reason=malformed_row",
                    exchange,
                    skipped,
                )
            if not table:
                _log.warning("flattrade_scrip_master_empty exchange=%s", exchange)
                return 0
            self._tsym[exchange] = tsyms
            self._instruments[exchange] = instruments
            self._tables[exchange] = table
            return len(table)

    async def resolve(self, symbol: str, default_exchange: str = "NSE") -> Resolved:
        """Resolve a bare or `EXCH:`-prefixed symbol; KeyError when unknown."""
        prefix, sep, bare = symbol.rpartition(":")
        exchange = prefix.upper() if sep and prefix else default_exchange
        if not self._tables.get(exchange):
            await self.load(exchange)
        hit = lookup(self._tables.get(exchange, {}), bare.strip())
        if hit is None:
            raise KeyError(f"no Flattrade token found for {symbol!r} on {exchange}")
        token, exch = hit
        return Resolved(token, exch, self._tsym[exch][token])

    async def instruments(self, exchange: str) -> list[Instrument]:
        """Instruments for one exchange (empty if the load failed)."""
        await self.load(exchange)
        return list(self._instruments.get(exchange, []))
