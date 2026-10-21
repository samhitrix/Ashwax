"""OHLCVRepository — ohlcv_candles table."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import asyncpg
    import pandas as pd


class OHLCVRepository:
    """Delta-aware OHLCV storage. upsert_candles is idempotent via ON CONFLICT DO UPDATE."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def latest_ts(self, symbol: str, timeframe: str) -> datetime | None:
        """Most-recent stored timestamp for (symbol, timeframe), or None if no rows exist."""
        async with self._pool.acquire() as conn:
            return cast(
                "datetime | None",
                await conn.fetchval(
                    "SELECT MAX(ts) FROM ohlcv_candles WHERE symbol=$1 AND timeframe=$2",
                    symbol,
                    timeframe,
                ),
            )

    async def earliest_ts(self, symbol: str, timeframe: str) -> datetime | None:
        """Oldest stored timestamp for (symbol, timeframe), or None if no rows exist.

        Used to detect shallow intraday history: delta-forward downloads never
        reach back past this bar, so backtests starve until a deep backfill runs.
        """
        async with self._pool.acquire() as conn:
            return cast(
                "datetime | None",
                await conn.fetchval(
                    "SELECT MIN(ts) FROM ohlcv_candles WHERE symbol=$1 AND timeframe=$2",
                    symbol,
                    timeframe,
                ),
            )

    async def upsert_candles(
        self, symbol: str, timeframe: str, rows: list[dict[str, Any]], source: str | None = None
    ) -> int:
        """Bulk-insert rows; update OHLCV (and provider ``source``) on conflict. Returns row count."""
        if not rows:
            return 0
        records = [
            (
                symbol,
                timeframe,
                row["ts"]
                if isinstance(row["ts"], datetime)
                else datetime.fromisoformat(str(row["ts"])),
                float(row.get("open", 0)),
                float(row.get("high", 0)),
                float(row.get("low", 0)),
                float(row.get("close", 0)),
                int(row.get("volume", 0)),
                source,
            )
            for row in rows
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO ohlcv_candles (symbol, timeframe, ts, open, high, low, close, volume, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (symbol, timeframe, ts) DO UPDATE
                    SET open=EXCLUDED.open, high=EXCLUDED.high,
                        low=EXCLUDED.low, close=EXCLUDED.close,
                        volume=EXCLUDED.volume, source=EXCLUDED.source
                """,
                records,
            )
        return len(records)

    async def latest_source(self, symbol: str, timeframe: str) -> str | None:
        """Source tag of the newest stored candle (None if untagged/no rows)."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(  # type: ignore[no-any-return]
                "SELECT source FROM ohlcv_candles WHERE symbol=$1 AND timeframe=$2 "
                "ORDER BY ts DESC LIMIT 1",
                symbol,
                timeframe,
            )

    async def adv_crores(self, days: int = 20) -> dict[str, float]:
        """Average DAILY traded value (close x volume) in Rs crore over each symbol's last `days`
        daily bars -- the liquidity measure the intraday menu filters on."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol, avg(close * volume) / 1e7 AS cr FROM (
                    SELECT symbol, close, volume,
                           row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
                      FROM ohlcv_candles WHERE timeframe = '1d'
                ) t WHERE rn <= $1 GROUP BY symbol
                """,
                days,
            )
        return {r["symbol"]: float(r["cr"] or 0.0) for r in rows}

    async def max_gap_pct(self, symbol: str, years: int = 2) -> float:
        """Max overnight gap over N years — used by gap risk manager per-symbol threshold.
        Returns 0.04 (4%) when insufficient OHLCV history exists."""
        async with self._pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT COALESCE(MAX(ABS(open - prev_close) / NULLIF(prev_close, 0)), 0.04)
                FROM (
                    SELECT open,
                           LAG(close) OVER (ORDER BY ts) AS prev_close
                    FROM ohlcv_candles
                    WHERE symbol = $1
                      AND timeframe = '1d'
                      AND ts >= NOW() - ($2 * INTERVAL '1 year')
                ) sub
                WHERE prev_close IS NOT NULL AND prev_close > 0
                """,
                symbol,
                years,
            )
        return float(result) if result is not None else 0.04

    async def fetch_candles(
        self, symbol: str, timeframe: str, limit: int = 400
    ) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, open, high, low, close, volume
                FROM ohlcv_candles
                WHERE symbol=$1 AND timeframe=$2
                ORDER BY ts DESC
                LIMIT $3
                """,
                symbol,
                timeframe,
                limit,
            )
            return cast("list[asyncpg.Record]", rows)

    async def fetch_dataframe(self, symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
        import pandas as _pd

        rows = await self.fetch_candles(symbol, timeframe, limit)
        if not rows:
            return _pd.DataFrame()
        df = _pd.DataFrame([dict(r) for r in rows])
        df = df.sort_values("ts").set_index("ts")
        df.index.name = "date"
        # ohlcv_candles' numeric columns are Postgres NUMERIC, which asyncpg
        # returns as Decimal — pandas then stores them as object dtype, and any
        # downstream arithmetic against a plain float (an R-multiple, a config
        # value, ...) raises TypeError, since Python deliberately refuses to mix
        # Decimal and float (found 2026-09-08: vcp_swing's `risk * target_r`
        # crashed the scan every cycle this way). This changes the Python type
        # only — the numeric value is unchanged, and float64 has far more
        # precision than an NSE price ever needs. Not a "fix" to the price
        # itself: the tick-size-driven precision on OHLC is real and must never
        # be altered (see reference_nse_tick_size_not_corruption).
        for _col in ("open", "high", "low", "close", "volume"):
            if _col in df.columns:
                df[_col] = df[_col].astype(float)
        return df
