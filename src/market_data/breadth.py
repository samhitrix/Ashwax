"""Market breadth: fraction of the stock universe closing above its own N-day SMA.

Feeds MarketFilter(mode="breadth") — see docs/hypotheses/D_breadth_gated_breakout.md.
Same definition as the research test (scripts/research/test_d_breakout.py): every
non-index symbol (no space in the name) with a full SMA window that day, and a day
counts only when at least `min_symbols` symbols are measurable.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd

_log = logging.getLogger(__name__)

_SQL = """
WITH c AS (
    SELECT symbol,
           (ts AT TIME ZONE 'Asia/Kolkata')::date AS d,
           close,
           avg(close) OVER w AS sma,
           count(*)   OVER w AS n
    FROM ohlcv_candles
    WHERE timeframe = '1d' AND symbol NOT LIKE '% %'
      AND ts >= now() - ($2::int * interval '1 day')
    WINDOW w AS (PARTITION BY symbol ORDER BY ts ROWS BETWEEN $1::int - 1 PRECEDING AND CURRENT ROW)
)
SELECT d, avg((close > sma)::int)::float AS frac, count(*) AS k
FROM c WHERE n = $1::int
GROUP BY d HAVING count(*) >= $3::int
ORDER BY d
"""


async def fetch_breadth_series(
    pool: Any, sma_period: int = 200, lookback_days: int = 800, min_symbols: int = 100
) -> pd.Series:
    """Return a date-indexed breadth series (0..1). Empty on failure (logged) so the
    breadth gate fails CLOSED rather than trading on an unknown market state."""
    try:
        rows = await pool.fetch(_SQL, sma_period, lookback_days + sma_period * 2, min_symbols)
    except Exception as exc:
        _log.error(
            "breadth_fetch_failed err=%s: %s — breadth gate will block", type(exc).__name__, exc
        )
        return pd.Series(dtype=float)
    s = pd.Series({r["d"]: float(r["frac"]) for r in rows}, dtype=float).sort_index()
    if s.empty:
        _log.warning("breadth_series_empty sma=%d — breadth gate will block", sma_period)
    else:
        _log.info(
            "breadth_series sma=%d days=%d last=%s value=%.3f",
            sma_period,
            len(s),
            s.index[-1],
            s.iloc[-1],
        )
    return s


def breadth_from_closes(
    closes: pd.DataFrame, sma_period: int = 200, min_symbols: int = 100
) -> pd.Series:
    """Pure equivalent of the SQL above for tests/research: `closes` is a
    date x symbol frame of daily closes."""
    sma = closes.rolling(sma_period).mean()
    valid = sma.notna() & closes.notna()
    above = (closes > sma) & valid
    k = valid.sum(axis=1)
    frac = above.sum(axis=1) / k.where(k > 0)
    frac = frac[k >= min_symbols]
    frac.index = [d if isinstance(d, date) else pd.Timestamp(d).date() for d in frac.index]
    return frac
