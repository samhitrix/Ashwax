"""
Replay closed intraday (MIS) trades under the CURRENT vs PROPOSED exit settings.

Each closed trade's real entry, initial stop, target and direction are replayed bar
by bar on the stored 5-minute candles through the same SmartExitTracker the live
engine uses, once with llm_intraday_agent's current exit_conditions and once with
the giveback lock + session-clock decay added. Entries are held fixed, so the
difference is purely the exit design.

Also reports the entry reachability check (target_pct <= reach_mult x 5-min ATR% x
sqrt(bars left)) as a counterfactual: which trades it would have removed and what
those trades actually made.

Costs: 0.10% of entry per round trip, expressed in R and deducted from every trade.

Usage:
    python scripts/replay_intraday_exits.py
    python scripts/replay_intraday_exits.py --strategy llm_intraday_agent
"""

from __future__ import annotations

# Standalone analysis script: sys.path bootstrap precedes imports.
# ruff: noqa: E402
# mypy: ignore-errors
import argparse
import asyncio
import copy
import json
import math
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg
import pandas as pd
from src.strategies.indicators.library import compute
from src.strategies.llm_intraday.pipeline import reachable_target_pct
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import SmartExitTracker, USSCompiler
from src.strategies.uss.schema import Direction

from scripts.strategy_diagnosis import db_url

IST = "Asia/Kolkata"
SPEC_PATH = ROOT / "strategies/builtin/llm_intraday_agent.json"
PROPOSED = {
    "giveback_lock": {"activate_mfe_r": 0.5, "lock_fraction": 0.5},
    "time_decay": [
        {"at": "14:15", "min_mfe_r": 0.3, "lock_r": 0.0},
        {"at": "14:45", "lock_r": -0.5},
    ],
}
EXIT_START_MIN = 15 * 60 + 10  # 15:10 flat-by
COST_PCT = 0.10
REACH_MULT = 1.0

_SQL = """
SELECT agent_trade_id, symbol, direction, strategy_name, fill_price, initial_sl,
       initial_target, exit_price, entry_timestamp, fill_timestamp, '{src}' AS src
  FROM {table}
 WHERE order_state = 'CLOSED' AND product = 'MIS'
   AND fill_price IS NOT NULL AND initial_sl IS NOT NULL AND initial_target IS NOT NULL
"""


def _spec(extra: dict | None):
    raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["exit_conditions"].update(extra or {})
    return USSCompiler().compile(JSONParser().parse(raw)).spec


async def _bars(pool, symbol: str, entry_ts) -> pd.DataFrame:
    day_ist = entry_ts.astimezone(pd.Timestamp.now(tz=IST).tz).date()
    start = pd.Timestamp(day_ist, tz=IST) - timedelta(days=6)
    end = pd.Timestamp(day_ist, tz=IST) + timedelta(days=1)
    rows = await pool.fetch(
        "SELECT ts, open, high, low, close, volume FROM ohlcv_candles "
        "WHERE symbol=$1 AND timeframe='5m' AND ts >= $2 AND ts < $3 ORDER BY ts",
        symbol,
        start.to_pydatetime(),
        end.to_pydatetime(),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows]).set_index("ts")
    df.index = pd.DatetimeIndex(df.index).tz_convert(IST)
    return df.astype(float)


def _ride(spec, df: pd.DataFrame, entry_i: int, entry, sl, tgt, direction):
    """Step bars after the entry bar; returns (exit_price, reason, mfe_r)."""
    t = SmartExitTracker(spec, entry, sl, tgt, direction)
    risk = abs(entry - sl)
    mfe = 0.0
    day = df.index[entry_i].date()
    last = entry_i
    for i in range(entry_i + 1, len(df)):
        ts = df.index[i]
        if ts.date() != day:
            break
        last = i
        hi, lo = float(df["high"].iloc[i]), float(df["low"].iloc[i])
        move = (entry - lo) if direction == Direction.SHORT else (hi - entry)
        mfe = max(mfe, move / risk)
        in_exit = ts.hour * 60 + ts.minute >= EXIT_START_MIN
        be = t.step(df.iloc[: i + 1], in_exit)
        if be.exit:
            return be.exit_price, be.reason, mfe
    return float(df["close"].iloc[last]), "EOD (data end)", mfe


def _r(entry, exit_px, risk, short) -> float:
    gross = (entry - exit_px) if short else (exit_px - entry)
    return (gross - entry * COST_PCT / 100.0) / risk


def _stats(rs: list[float]) -> str:
    if not rs:
        return "n=0"
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    return (
        f"n={len(rs)} exp={sum(rs) / len(rs):+.3f}R win%={len(wins) / len(rs) * 100:.0f} "
        f"PF={pf:.2f} avgW={avg_w:+.2f}R avgL={avg_l:+.2f}R total={sum(rs):+.1f}R"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None, help="filter by strategy_name")
    args = ap.parse_args()

    cur_spec, new_spec = _spec(None), _spec(PROPOSED)
    pool = await asyncpg.create_pool(db_url(), min_size=1, max_size=4)
    try:
        rows = list(await pool.fetch(_SQL.format(table="paper_trades", src="paper")))
        rows += list(await pool.fetch(_SQL.format(table="trades", src="live")))
        if args.strategy:
            rows = [r for r in rows if r["strategy_name"] == args.strategy]

        cur_r, new_r, skipped = [], [], 0
        cur_red_after_half, new_red_after_half, reached_half = 0, 0, 0
        reach_removed: list[float] = []
        reach_kept: list[float] = []
        by_reason_new: dict[str, int] = {}
        for r in rows:
            entry = float(r["fill_price"])
            sl = float(r["initial_sl"])
            tgt = float(r["initial_target"])
            short = str(r["direction"]).lower() == "short"
            risk = abs(entry - sl)
            if risk <= 0:
                skipped += 1
                continue
            ets = r["fill_timestamp"] or r["entry_timestamp"]
            df = await _bars(pool, r["symbol"], ets)
            if df.empty:
                skipped += 1
                continue
            ets_ist = pd.Timestamp(ets).tz_convert(IST)
            pos = df.index.searchsorted(ets_ist, side="right") - 1
            if pos < 15 or df.index[pos].date() != ets_ist.date():
                skipped += 1
                continue
            d = Direction.SHORT if short else Direction.LONG
            cx, _, mfe = _ride(cur_spec, df, pos, entry, sl, tgt, d)
            nx, nreason, _ = _ride(new_spec, df, pos, entry, sl, tgt, d)
            rc, rn = _r(entry, cx, risk, short), _r(entry, nx, risk, short)
            cur_r.append(rc)
            new_r.append(rn)
            by_reason_new[nreason] = by_reason_new.get(nreason, 0) + 1
            if mfe >= 0.5:
                reached_half += 1
                cur_red_after_half += rc <= 0
                new_red_after_half += rn <= 0

            atr = float(compute(df.iloc[: pos + 1], "ATR", period=14).iloc[-1])
            removed = False
            if math.isfinite(atr) and atr > 0:
                mins_left = EXIT_START_MIN - (ets_ist.hour * 60 + ets_ist.minute)
                reach = reachable_target_pct(atr / entry * 100.0, mins_left, REACH_MULT)
                removed = abs(tgt - entry) / entry * 100.0 > reach
            (reach_removed if removed else reach_kept).append(rc)

        scope = args.strategy or "all MIS strategies"
        print(f"\nReplay: {scope} | trades={len(cur_r)} skipped(no bars/data)={skipped}")
        print(f"  current exits : {_stats(cur_r)}")
        print(f"  proposed exits: {_stats(new_r)}")
        print(
            f"  trades that reached +0.5R: {reached_half} | ended red: current="
            f"{cur_red_after_half} proposed={new_red_after_half}"
        )
        print(f"  proposed exit reasons: {by_reason_new}")
        print(
            f"  reachability x{REACH_MULT} (current exits): would remove "
            f"{len(reach_removed)} [{_stats(reach_removed)}]; kept [{_stats(reach_kept)}]"
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
