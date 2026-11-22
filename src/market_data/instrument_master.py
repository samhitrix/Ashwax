"""NSE instrument master — the authoritative set of currently-tradeable symbols.

The trade/download universe is sourced from static lists (hardcoded NIFTY50,
cached nifty500_symbols.json) that are never reconciled with what the broker
actually lists. Delisted/renamed symbols linger there and get fetched/traded,
producing empty responses and wasted API calls. This module intersects any
universe against the broker's live instrument dump so invalid symbols are
dropped once, up front, instead of failing one-by-one.

Daily-cached to data/nse_instruments.json (one fetch per trading day). Fails
OPEN: if the master can't be loaded the configured universe is kept unchanged —
never silently zeroed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_BASE = Path(__file__).parent.parent.parent
_CACHE = _BASE / "data" / "nse_instruments.json"
_IST = ZoneInfo("Asia/Kolkata")
_log = logging.getLogger(__name__)  # "src.market_data.instrument_master" -> market_data.log

# Below this the dump is clearly wrong (auth/paper stub) — keep the universe.
_MIN_PLAUSIBLE = 100


def _today() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _read_cache(*, require_today: bool) -> set[str] | None:
    if not _CACHE.exists():
        return None
    try:
        blob = json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if require_today and blob.get("date") != _today():
        return None
    syms = blob.get("symbols", [])
    return set(syms) if len(syms) >= _MIN_PLAUSIBLE else None


def _write_cache(symbols: set[str]) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(
            json.dumps({"date": _today(), "symbols": sorted(symbols)}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # cache is an optimization, never fatal
        _log.warning("instrument-master cache write failed: %s", exc)


def cached_valid_symbols() -> set[str] | None:
    """Synchronous read of today's cached tradeable-symbol set, no broker call.

    For lightweight validation call sites (e.g. the news pipeline, which runs
    on its own polling loop and shouldn't wait on/trigger a broker fetch).
    Returns None when there's no fresh cache yet — callers should fail open.
    """
    return _read_cache(require_today=True)


async def valid_nse_symbols(adapter: object, *, refresh: bool = False) -> set[str] | None:
    """Set of currently-tradeable NSE *equity* tradingsymbols (daily-cached).

    Returns None when the master is unavailable so callers fail OPEN (keep their
    configured universe) rather than trading an empty list.
    """
    if not refresh:
        fresh = _read_cache(require_today=True)
        if fresh is not None:
            return fresh
    try:
        instruments = await adapter.get_instruments("NSE")  # type: ignore[attr-defined]
    except Exception as exc:
        _log.warning("instrument fetch failed (%s) — using stale cache / keeping universe", exc)
        return _read_cache(require_today=False)
    symbols = {
        ins.symbol
        for ins in instruments
        if getattr(ins, "instrument_type", "") == "EQ" and getattr(ins, "exchange", "") == "NSE"
    }
    if len(symbols) < _MIN_PLAUSIBLE:
        _log.warning("instrument master too small (%d) — keeping configured universe", len(symbols))
        return _read_cache(require_today=False)
    _write_cache(symbols)
    _log.info("instrument master: %d tradeable NSE EQ symbols", len(symbols))
    return symbols


def filter_universe(symbols: list[str], valid: set[str] | None) -> list[str]:
    """Drop symbols absent from the instrument master. valid=None → fail open.

    Symbols may carry an ``NSE:`` prefix; matching is done on the bare symbol.
    """
    if not valid:
        return symbols

    def _bare(s: str) -> str:
        return s.split(":", 1)[1] if ":" in s else s

    kept = [s for s in symbols if _bare(s) in valid]
    dropped = [s for s in symbols if _bare(s) not in valid]
    if dropped:
        _log.info(
            "dropped %d invalid/delisted symbol(s): %s",
            len(dropped),
            ",".join(_bare(s) for s in dropped),
        )
    return kept
