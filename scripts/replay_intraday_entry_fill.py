"""
Replay llm_intraday_agent's ENTRY-FILL rule (not the exit rule) against every idea it has
ever proposed (llm_intraday_journal, decision='proposed'), to test whether the current flat
entry_tolerance_pct=0.4% / idea_ttl_minutes=15 window is why ideas expire untraded, and
whether a wider or ATR-scaled tolerance would have produced more fills without ruining
expectancy.

Each idea's entry/stop/target are held fixed (same simplification as
replay_intraday_exits.py) -- only the FILL TRIGGER varies between variants. A fill happens
at the first 5-min bar, within idea_ttl_minutes of the idea's timestamp, whose close is
within `tolerance` of the quoted entry_price. idea['atr14_pct'] (logged at idea creation) is
used as the ATR term rather than recomputed per-bar -- a reasonable approximation for this
comparison, not a live-trading number. Once filled, the trade rides forward through the
SAME SmartExitTracker exit logic as the current live spec (via replay_intraday_exits._ride),
so only the entry-fill variable changes between runs.

Costs: 0.10% of entry per round trip (matches replay_intraday_exits.py), deducted in R.

Usage:
    python scripts/replay_intraday_entry_fill.py
"""

from __future__ import annotations

# Standalone analysis script: sys.path bootstrap precedes imports.
# ruff: noqa: E402
# mypy: ignore-errors
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg
import pandas as pd
from src.strategies.uss.schema import Direction

from scripts.replay_intraday_exits import IST, _bars, _r, _ride, _spec, _stats
from scripts.strategy_diagnosis import db_url

VARIANTS = [
    ("current (0.40% flat, ttl 15m)", 0.40, 0.0, 15),
    ("wider flat (0.60% flat, ttl 15m)", 0.60, 0.0, 15),
    ("ATR x0.75 (floor 0.40%, ttl 15m)", 0.40, 0.75, 15),
    ("ATR x1.00 (floor 0.40%, ttl 20m)", 0.40, 1.00, 20),
    ("ATR x1.25 (floor 0.40%, ttl 30m)", 0.40, 1.25, 30),
]


async def _load_ideas(pool) -> list[dict]:
    rows = await pool.fetch(
        "SELECT ideas_json FROM llm_intraday_journal WHERE decision = 'proposed' ORDER BY id"
    )
    ideas = []
    for r in rows:
        raw = r["ideas_json"]
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or [])
        for idea in parsed:
            if all(
                k in idea
                for k in ("symbol", "direction", "entry_price", "stop_loss", "target", "ts")
            ):
                ideas.append(idea)
    return ideas


def _tolerance_pct(floor_pct: float, atr_mult: float, atr14_pct: float | None) -> float:
    if atr_mult <= 0 or atr14_pct is None:
        return floor_pct
    return max(floor_pct, atr_mult * float(atr14_pct))


async def _find_fill(
    df: pd.DataFrame, idea_ts: pd.Timestamp, entry: float, tol_pct: float, ttl_min: int
):
    """First bar index whose close lands within tol_pct of entry, within ttl_min of idea_ts."""
    idea_ts = idea_ts.as_unit(df.index.dtype.unit)
    start = df.index.searchsorted(idea_ts, side="right")
    deadline = idea_ts + pd.Timedelta(minutes=ttl_min)
    for i in range(start, len(df)):
        ts = df.index[i]
        if ts > deadline:
            return None
        close = float(df["close"].iloc[i])
        drift = abs(close - entry) / entry * 100.0 if entry > 0 else float("inf")
        if drift <= tol_pct:
            return i
    return None


async def main() -> None:
    spec = _spec(None)
    pool = await asyncpg.create_pool(db_url(), min_size=1, max_size=4)
    try:
        ideas = await _load_ideas(pool)
        print(f"Loaded {len(ideas)} proposed ideas from llm_intraday_journal")

        # Pre-fetch candles once per idea (symbol+day), reused across variants.
        bars_cache: dict[tuple, pd.DataFrame] = {}
        for idea in ideas:
            idea_ts = pd.Timestamp(float(idea["ts"]), unit="s", tz="UTC").tz_convert(IST)
            key = (idea["symbol"], idea_ts.date())
            if key not in bars_cache:
                bars_cache[key] = await _bars(pool, idea["symbol"], idea_ts)

        for label, floor_pct, atr_mult, ttl_min in VARIANTS:
            rs: list[float] = []
            filled = 0
            no_data = 0
            for idea in ideas:
                idea_ts = pd.Timestamp(float(idea["ts"]), unit="s", tz="UTC").tz_convert(IST)
                df = bars_cache[(idea["symbol"], idea_ts.date())]
                if df.empty:
                    no_data += 1
                    continue
                entry = float(idea["entry_price"])
                sl = float(idea["stop_loss"])
                tgt = float(idea["target"])
                short = str(idea["direction"]).upper() == "SHORT"
                tol = _tolerance_pct(floor_pct, atr_mult, idea.get("atr14_pct"))
                fill_i = await _find_fill(df, idea_ts, entry, tol, ttl_min)
                if fill_i is None:
                    continue
                filled += 1
                risk = abs(entry - sl)
                if risk <= 0:
                    continue
                d = Direction.SHORT if short else Direction.LONG
                exit_px, _reason, _mfe = _ride(spec, df, fill_i, entry, sl, tgt, d)
                rs.append(_r(entry, exit_px, risk, short))

            print(f"\n{label}")
            print(f"  fills: {filled}/{len(ideas)} (no candle data: {no_data})")
            print(f"  outcome of filled trades: {_stats(rs)}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
