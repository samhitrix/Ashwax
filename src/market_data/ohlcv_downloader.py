"""
OHLCV historical data downloader.

Delta-aware: queries DB for latest stored timestamp per (symbol, timeframe).
- First run (no DB rows): bootstrap — fetches full history.
- Subsequent runs: fetches only candles newer than latest_ts, skips if already current.
- Gap detection is automatic: from_dt covers any missed days since last run.

Timeframes: 1d, 1w, 15m, 10m, 5m (Parquet cache kept for 1d as fallback).

Usage:
    python -m src.market_data.ohlcv_downloader               # uses config defaults
    python -m src.market_data.ohlcv_downloader --universe nifty50
    python -m src.market_data.ohlcv_downloader --symbols INFY,TCS,RELIANCE
    python -m src.market_data.ohlcv_downloader --days 1825
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_BASE = Path(__file__).parent.parent.parent
_DATA_DIR = _BASE / "data" / "ohlcv" / "day"
_ENV = _BASE / ".env"
_IST = ZoneInfo("Asia/Kolkata")
_NIFTY100_CACHE = _BASE / "data" / "nifty100_symbols.json"
_NIFTY200_CACHE = _BASE / "data" / "nifty200_symbols.json"
_NIFTY500_CACHE = _BASE / "data" / "nifty500_symbols.json"
_NSE_NIFTY100_CSV = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
_NSE_NIFTY200_CSV = "https://archives.nseindia.com/content/indices/ind_nifty200list.csv"
_NSE_NIFTY500_CSV = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"


# (kite_interval, timeframe_key, bootstrap_candles, is_intraday)
# 5m/10m day-depth is driven by universe.json ohlcv.intraday_days_rolling, capped
# at the Kite per-interval hard limit. 15m keeps its own 200-day Kite ceiling.
def build_timeframes(intraday_days: int | None = None) -> list[tuple[str, str, int, bool]]:
    if intraday_days is None:
        intraday_days = int(_load_config().get("ohlcv", {}).get("intraday_days_rolling", 100))
    short_depth = min(intraday_days, 100)  # 5m/10m Kite hard limit
    return [
        ("day", "1d", 2000, False),
        ("week", "1w", 2000, False),
        ("15minute", "15m", 200, True),  # Kite hard limit: 200 days
        ("10minute", "10m", short_depth, True),
        ("5minute", "5m", short_depth, True),
    ]


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger(__name__)  # "src.market_data.ohlcv_downloader" -> market_data.log

NIFTY50: list[str] = [
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BPCL",
    "BHARTIARTL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DIVISLAB",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "ITC",
    "INDUSINDBK",
    "INFY",
    "JSWSTEEL",
    "KOTAKBANK",
    "LTIM",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SUNPHARMA",
    "TCS",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TECHM",
    "TITAN",
    "ULTRACEMCO",
    "WIPRO",
    "ZOMATO",
]


def _load_config() -> dict[str, Any]:
    p = _BASE / "config" / "universe.json"
    if p.exists():
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    return {}


def _load_global_config() -> dict[str, Any]:
    p = _BASE / "config" / "global.json"
    if p.exists():
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    return {}


# Backward-compatible module constant; built once from config at import. Consumers
# that must reflect a live config change call build_timeframes() directly instead.
_TIMEFRAMES = build_timeframes()


def _fetch_index_symbols(
    label: str, csv_url: str, cache_path: Path, min_expected: int
) -> list[str]:
    """Fetch an NSE index's constituents from its public CSV. Caches to cache_path."""
    if cache_path.exists():
        try:
            syms: list[str] = json.loads(cache_path.read_text(encoding="utf-8"))
            _log.info("loaded %d %s symbols from cache", len(syms), label)
            return syms
        except Exception:
            pass
    _log.info("fetching %s symbol list from NSE...", label)
    try:
        import urllib.request

        req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        # NSE's own index CSVs carry temporary "DUMMY*" when-issued placeholder
        # symbols during a demerger/spin-off reconstitution (e.g. DUMMYVEDL1-4 for
        # the Vedanta demerger) — these have no instrument token/historical data on
        # Kite and fail every OHLCV fetch forever, every night, until NSE removes
        # them (found 2026-09-08: HEG/HFCL/JBCHEPHARM's real "fetch failed" noise
        # was masking these 4 permanently-broken entries in the same log pattern).
        symbols = [
            row["Symbol"].strip()
            for row in reader
            if row.get("Symbol", "").strip() and not row["Symbol"].strip().startswith("DUMMY")
        ]
        if len(symbols) < min_expected:
            raise ValueError(f"too few symbols parsed: {len(symbols)}")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(symbols, indent=2), encoding="utf-8")
        _log.info("fetched and cached %d %s symbols", len(symbols), label)
        return symbols
    except Exception as exc:
        _log.warning("%s fetch failed (%s) — falling back to nifty50", label, exc)
        return NIFTY50


def _fetch_nifty100_symbols() -> list[str]:
    return _fetch_index_symbols("nifty100", _NSE_NIFTY100_CSV, _NIFTY100_CACHE, min_expected=80)


def _fetch_nifty200_symbols() -> list[str]:
    return _fetch_index_symbols("nifty200", _NSE_NIFTY200_CSV, _NIFTY200_CACHE, min_expected=150)


def _fetch_nifty500_symbols() -> list[str]:
    return _fetch_index_symbols("nifty500", _NSE_NIFTY500_CSV, _NIFTY500_CACHE, min_expected=100)


def _parquet_path(symbol: str) -> Path:
    return _DATA_DIR / f"{symbol}.parquet"


def _df_to_rows(df: object) -> list[dict[str, Any]]:
    """Convert broker DataFrame to list[dict] suitable for upsert_candles."""
    import pandas as _pd

    df = df.reset_index() if hasattr(df, "reset_index") else df
    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():  # type: ignore[attr-defined]
        ts = r.get("date", None)
        if ts is None:
            continue
        if not isinstance(ts, datetime):
            ts = _pd.Timestamp(ts).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_IST)
        rows.append(
            {
                "ts": ts,
                "open": float(r.get("open", 0)),
                "high": float(r.get("high", 0)),
                "low": float(r.get("low", 0)),
                "close": float(r.get("close", 0)),
                "volume": int(r.get("volume", 0)),
            }
        )
    return rows


async def _download_symbol_timeframe(
    adapter: object,
    ohlcv_repo: object | None,
    symbol: str,
    kite_interval: str,
    tf_key: str,
    bootstrap_candles: int,
    is_intraday: bool,
    sem: asyncio.Semaphore,
    stale_threshold_minutes: int | None = None,
) -> bool:
    """Download and store one (symbol, timeframe). Returns True on success.

    stale_threshold_minutes: override the default skip threshold (5 min intraday /
    1440 min daily). Pass a larger value (e.g. 23*60) when calling outside market
    hours so data downloaded earlier the same day is not re-fetched.
    """
    async with sem:
        try:
            last_ts: datetime | None = None
            earliest_ts: datetime | None = None
            if ohlcv_repo is not None:
                last_ts = await ohlcv_repo.latest_ts(symbol, tf_key)  # type: ignore[attr-defined]
                if is_intraday:
                    earliest_ts = await ohlcv_repo.earliest_ts(symbol, tf_key)  # type: ignore[attr-defined]

            now = datetime.now(_IST)

            # Deep historical backfill (intraday only). Delta-forward updates never
            # reach BACK past the earliest stored bar — so once live ticks seed the
            # DB with a few recent days, the bootstrap window is never pulled again
            # and backtests starve on ~1 week of data. If the oldest stored bar is
            # shallower than the target depth, re-fetch the full window; the
            # idempotent upsert fills the gap without duplicating existing bars.
            needs_deep_backfill = is_intraday and (
                earliest_ts is None
                or (earliest_ts if earliest_ts.tzinfo else earliest_ts.replace(tzinfo=_IST))
                > now - timedelta(days=bootstrap_candles - 5)
            )

            if needs_deep_backfill:
                # bootstrap_candles is the intraday day-depth (5m/10m=100, 15m=200).
                candle_count = bootstrap_candles
            elif last_ts is not None:
                # Delta: only fetch candles newer than last stored timestamp
                last_ts_aware = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=_IST)
                _threshold = (
                    stale_threshold_minutes
                    if stale_threshold_minutes is not None
                    else (5 if is_intraday else 60 * 24)
                )
                if last_ts_aware >= now - timedelta(minutes=_threshold):
                    _log.debug("skip %-20s %-4s already current", symbol, tf_key)
                    return True
                candle_count = max(2, (now - last_ts_aware).days + 2)
            else:
                # Bootstrap: first run, fetch full history within Kite limits
                candle_count = bootstrap_candles

            df_new = await adapter.get_ohlcv(f"NSE:{symbol}", kite_interval, candle_count)  # type: ignore[attr-defined]
            if df_new is None or df_new.empty:
                _log.warning("empty response %-20s %-4s", symbol, tf_key)
                return False

            df_new.columns = [c.lower() for c in df_new.columns]

            # DB write
            if ohlcv_repo is not None:
                rows = _df_to_rows(df_new)
                if rows:
                    n = await ohlcv_repo.upsert_candles(symbol, tf_key, rows)  # type: ignore[attr-defined]
                    _log.info("db upsert %-20s %-4s %d rows", symbol, tf_key, n)

            _log.info("ok %-20s %-4s %d rows", symbol, tf_key, len(df_new))
            return True

        except Exception as exc:
            _log.warning("fetch failed %-20s %-4s %s", symbol, tf_key, exc)
            return False


async def _run(
    symbols: list[str],
    days: int,
    concurrency: int,
    db_pool: object | None = None,
) -> None:
    from src.brokers.adapters.kite.adapter import KiteAdapter
    from src.brokers.adapters.kite.auth import KiteAuth
    from src.brokers.kite_auth import auto_refresh

    try:
        auto_refresh(_ENV)
    except Exception as exc:
        _log.warning("kite_auth: %s — using cached token", exc)

    adapter = KiteAdapter(KiteAuth.from_env())

    # Reconcile the universe against the broker's live instrument master so
    # delisted/renamed symbols are never fetched (fails open if unavailable).
    from src.market_data.instrument_master import filter_universe, valid_nse_symbols

    valid = await valid_nse_symbols(adapter)
    symbols = filter_universe(symbols, valid)
    _log.info("downloading %d valid symbols after instrument-master filter", len(symbols))

    sem = asyncio.Semaphore(concurrency)

    ohlcv_repo = None
    if db_pool is not None:
        from src.db.repositories.ohlcv import OHLCVRepository

        ohlcv_repo = OHLCVRepository(db_pool)

    tasks = [
        _download_symbol_timeframe(
            adapter,
            ohlcv_repo,
            sym,
            kite_interval,
            tf_key,
            bootstrap_candles,
            is_intraday,
            sem,
        )
        for sym in symbols
        for kite_interval, tf_key, bootstrap_candles, is_intraday in _TIMEFRAMES
    ]
    results = await asyncio.gather(*tasks)
    ok = sum(results)
    total = len(tasks)
    _log.info("done — %d/%d ok, %d failed", ok, total, total - ok)


def main() -> None:
    cfg = _load_config()
    ohlcv_cfg = cfg.get("ohlcv", {})
    default_universe = cfg.get("cache_universe", "NIFTY50").lower()
    # History window: global.json equity.screener.ohlcv_history_years is the screener
    # authority; universe.json ohlcv.history_years is the fallback.
    _screener_cfg = _load_global_config().get("equity", {}).get("screener", {})
    _history_years = int(
        _screener_cfg.get("ohlcv_history_years", ohlcv_cfg.get("history_years", 5))
    )
    default_days = _history_years * 365
    default_concurrency = int(ohlcv_cfg.get("max_concurrent_fetches", 5))

    parser = argparse.ArgumentParser(description="Download NSE OHLCV data (delta-aware)")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--universe", default=default_universe, choices=["nifty50", "nifty500"])
    grp.add_argument("--symbols", help="Comma-separated symbols, e.g. INFY,TCS")
    parser.add_argument("--days", type=int, default=default_days)
    parser.add_argument("--concurrency", type=int, default=default_concurrency)
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    elif args.universe == "nifty500":
        symbols = _fetch_nifty500_symbols()
    else:
        symbols = NIFTY50

    _log.info(
        "downloading %d symbols × 5 timeframes (concurrency=%d)", len(symbols), args.concurrency
    )

    async def _main_with_db() -> None:
        from dotenv import load_dotenv

        load_dotenv(_ENV)
        db_pool = None
        try:
            from src.db.migrate import run_migrations
            from src.db.pool import init_pool

            db_pool = await init_pool()
            await run_migrations(db_pool)  # creates ohlcv_candles table if not exists
            _log.info("db pool ready — writes will go to ohlcv_candles")
        except Exception as exc:
            _log.warning("db pool unavailable (%s) — parquet-only mode", exc)
        try:
            await _run(symbols, args.days, args.concurrency, db_pool=db_pool)
        finally:
            if db_pool is not None:
                from src.db.pool import close_pool

                await close_pool()

    asyncio.run(_main_with_db())


if __name__ == "__main__":
    main()
