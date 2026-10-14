"""NSE sector index constituent lists, for mapping a stock symbol to its sector.

Modeled on ohlcv_downloader.py's `_fetch_index_symbols`: same NSE public-archive
CSV fetch, same JSON cache-file pattern. Needed for the VCP swing pipeline's
stage 1 -> stage 2 handoff (restrict the stock universe to symbols belonging to
the leading sectors) since no sector metadata exists anywhere else in this repo
(data/nse_instruments.json is a flat, untyped symbol list).

Each sector's CSV fetch fails soft: a bad/changed URL for one sector drops only
that sector from the result (logged), it does not abort the whole fetch. All
15 URLs below were fetched and confirmed live against the real NSE archive on
2026-09-01 (each returned a distinct, real constituent CSV) — this was
previously an open item (inferred by analogy, unverified); now closed. NSE
occasionally reshuffles these slugs, so a future failure here is still
expected to be caught by the per-sector fail-soft path, not assumed permanent.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path

_BASE = Path(__file__).parent.parent.parent
_CACHE_PATH = _BASE / "data" / "sector_constituents.json"

_log = logging.getLogger(__name__)  # "src.market_data.sector_constituents" -> market_data.log

_SECTOR_CSV_URLS: dict[str, str] = {
    "NIFTY AUTO": "https://archives.nseindia.com/content/indices/ind_niftyautolist.csv",
    "NIFTY BANK": "https://archives.nseindia.com/content/indices/ind_niftybanklist.csv",
    "NIFTY IT": "https://archives.nseindia.com/content/indices/ind_niftyitlist.csv",
    "NIFTY FMCG": "https://archives.nseindia.com/content/indices/ind_niftyfmcglist.csv",
    "NIFTY METAL": "https://archives.nseindia.com/content/indices/ind_niftymetallist.csv",
    "NIFTY PHARMA": "https://archives.nseindia.com/content/indices/ind_niftypharmalist.csv",
    "NIFTY REALTY": "https://archives.nseindia.com/content/indices/ind_niftyrealtylist.csv",
    "NIFTY ENERGY": "https://archives.nseindia.com/content/indices/ind_niftyenergylist.csv",
    "NIFTY PSU BANK": "https://archives.nseindia.com/content/indices/ind_niftypsubanklist.csv",
    "NIFTY FIN SERVICE": "https://archives.nseindia.com/content/indices/ind_niftyfinancelist.csv",
    "NIFTY HEALTHCARE": "https://archives.nseindia.com/content/indices/ind_niftyhealthcarelist.csv",
    "NIFTY MEDIA": "https://archives.nseindia.com/content/indices/ind_niftymedialist.csv",
    "NIFTY CONSR DURBL": "https://archives.nseindia.com/content/indices/ind_niftyconsumerdurableslist.csv",
    "NIFTY OIL AND GAS": "https://archives.nseindia.com/content/indices/ind_niftyoilgaslist.csv",
    "NIFTY INFRA": "https://archives.nseindia.com/content/indices/ind_niftyinfralist.csv",
}
# All 15 URLs above were fetched and verified live (returned real, distinct
# constituent CSVs) on 2026-09-01 — unlike the original 10, which were only
# inferred by naming-convention analogy. NIFTY CHEMICALS and NIFTY CAPITAL
# MKT (also valid instrument symbols per data/nse_instruments.json) were
# tried under several slug guesses and found no public archive CSV — omitted
# rather than guessed.


def _fetch_one_sector_csv(sector: str, csv_url: str) -> list[str] | None:
    try:
        import urllib.request

        req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        symbols = [row["Symbol"].strip() for row in reader if row.get("Symbol", "").strip()]
        if not symbols:
            raise ValueError("no symbols parsed")
        return symbols
    except Exception as exc:
        _log.warning("sector_constituents_fetch_failed sector=%s err=%s", sector, exc)
        return None


def fetch_sector_constituents(force_refresh: bool = False) -> dict[str, list[str]]:
    """Return {sector_name: [member_symbols]} for the 10 NSE sector indices.

    Cached to data/sector_constituents.json. A sector whose CSV fetch fails is
    simply omitted from the result (and from the cache) rather than aborting
    the whole call — callers should treat a missing sector as "unknown
    membership", not crash.
    """
    if not force_refresh and _CACHE_PATH.exists():
        try:
            cached: dict[str, list[str]] = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if cached:
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    result: dict[str, list[str]] = {}
    for sector, url in _SECTOR_CSV_URLS.items():
        symbols = _fetch_one_sector_csv(sector, url)
        if symbols:
            result[sector] = symbols

    if result:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
        except OSError as exc:
            _log.warning("sector_constituents_cache_write_failed err=%s", exc)

    return result


def symbol_to_sector_map(constituents: dict[str, list[str]] | None = None) -> dict[str, str]:
    """Reverse {sector: [symbols]} into {symbol: sector}.

    A symbol listed under more than one sector index keeps whichever sector is
    encountered last (dict iteration order of `constituents`) — sector index
    membership is not expected to overlap for NSE's standard sectoral indices,
    but this is documented rather than silently arbitrary.
    """
    src = constituents if constituents is not None else fetch_sector_constituents()
    mapping: dict[str, str] = {}
    for sector, symbols in src.items():
        for sym in symbols:
            mapping[sym] = sector
    return mapping
