"""
Strategy diagnosis — full-window backtest + expectancy slice report.

Re-runs any USS strategy JSON over the FULL candle history in the DB (no
2500-bar cap, no 500-trade truncation) and reports WHERE expectancy is
gained/lost: by exit reason, direction, entry time-of-day, day-of-week,
symbol and month. Footer scores the combined trade list with the engine's
own PASS/FAIL thresholds.

All metrics are PRE-COST: BacktestEngine models no brokerage/STT/slippage.

Usage:
    python scripts/strategy_diagnosis.py                                # all enabled specs
    python scripts/strategy_diagnosis.py --spec strategies/builtin/minervini_vcp.json
    python scripts/strategy_diagnosis.py --spec <variant.json> --from 2026-03-02 --to 2026-05-31
    python scripts/strategy_diagnosis.py --spec <spec> --json-out out.json
"""

from __future__ import annotations

# Standalone dev/backtest script: imports follow sys.path bootstrap (E402) and the
# USS parser returns loosely-typed objects, so file-level ruff/mypy relaxations apply.
# ruff: noqa: E402
# mypy: ignore-errors
import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg
import pandas as pd
from src.strategies.backtest.engine import BacktestEngine
from src.strategies.parsers.json_parser import JSONParser
from src.strategies.uss.compiler import USSCompiler
from src.strategies.uss.validator import USSValidator

IST = "Asia/Kolkata"

NIFTY50 = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "HINDUNILVR",
    "ITC",
    "SBIN",
    "BHARTIARTL",
    "KOTAKBANK",
    "LT",
    "AXISBANK",
    "ASIANPAINT",
    "MARUTI",
    "BAJFINANCE",
    "HCLTECH",
    "WIPRO",
    "ULTRACEMCO",
    "TITAN",
    "POWERGRID",
    "NTPC",
    "ONGC",
    "TATASTEEL",
    "TECHM",
    "M&M",
    "TATACONSUM",
    "NESTLEIND",
    "SUNPHARMA",
    "SBILIFE",
    "BAJAJFINSV",
    "ADANIPORTS",
    "ADANIENT",
    "BPCL",
    "COALINDIA",
    "DIVISLAB",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HDFC",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "INDUSINDBK",
    "JSWSTEEL",
    "CIPLA",
    "BRITANNIA",
    "APOLLOHOSP",
    "BAJAJ-AUTO",
    "UPL",
]
NIFTY50 = list(dict.fromkeys(NIFTY50))

# meta.timeframe (USS enum) -> ohlcv_candles.timeframe
TF_MAP = {"5MIN": "5m", "15MIN": "15m", "1HOUR": "1h", "DAILY": "1d", "WEEKLY": "1w"}


def db_url() -> str:
    if os.environ.get("DB_URL"):
        return os.environ["DB_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DB_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DB_URL not found in environment or .env")


def compile_spec(spec_path: Path):
    spec_dict = json.loads(spec_path.read_text())
    spec = JSONParser().parse(spec_dict)
    USSValidator().validate(spec)
    return USSCompiler().compile(spec)


async def fetch_df(
    conn: asyncpg.Connection,
    symbol: str,
    timeframe: str,
    from_dt: datetime | None,
    to_dt: datetime | None,
) -> pd.DataFrame | None:
    q = """
        SELECT ts, open, high, low, close, volume
        FROM ohlcv_candles
        WHERE symbol=$1 AND timeframe=$2
          AND ($3::timestamptz IS NULL OR ts >= $3)
          AND ($4::timestamptz IS NULL OR ts <= $4)
        ORDER BY ts ASC
    """
    rows = await conn.fetch(q, symbol, timeframe, from_dt, to_dt)
    if not rows or len(rows) < 50:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _agg(trades: list[dict]) -> str:
    n = len(trades)
    if n == 0:
        return "n=0"
    rs = [t["pnl_r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    return f"n={n:<5} WR={wins / n * 100:5.1f}%  sumR={sum(rs):+8.2f}  avgR={sum(rs) / n:+.3f}"


def _slice(trades: list[dict], key) -> list[tuple[str, list[dict]]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        buckets[key(t)].append(t)
    return sorted(buckets.items())


def print_report(name: str, trades: list[dict], result, intraday: bool) -> None:
    print(f"\n{'=' * 78}")
    print(f"  {name} — {len(trades)} positions")
    print(f"{'=' * 78}")
    if not trades:
        print("  no trades")
        return

    print("\n-- by exit_reason --")
    for k, ts in sorted(
        _slice(trades, lambda t: t["exit_reason"] or "?"),
        key=lambda kv: sum(t["pnl_r"] for t in kv[1]),
    ):
        print(f"  {k:<22} {_agg(ts)}")

    print("\n-- by direction --")
    for k, ts in _slice(trades, lambda t: t["direction"]):
        print(f"  {k:<22} {_agg(ts)}")

    if intraday:
        print("\n-- by entry time (IST, 30-min buckets) --")
        for k, ts in _slice(trades, lambda t: t["tod"]):
            print(f"  {k:<22} {_agg(ts)}")

    print("\n-- by day of week --")
    order = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
    for k, ts in sorted(_slice(trades, lambda t: t["dow"]), key=lambda kv: order.get(kv[0], 9)):
        print(f"  {k:<22} {_agg(ts)}")

    print("\n-- by month --")
    for k, ts in _slice(trades, lambda t: t["month"]):
        print(f"  {k:<22} {_agg(ts)}")

    sym_slices = _slice(trades, lambda t: t["symbol"])
    sym_slices.sort(key=lambda kv: sum(t["pnl_r"] for t in kv[1]))
    print("\n-- bottom 10 symbols --")
    for k, ts in sym_slices[:10]:
        print(f"  {k:<22} {_agg(ts)}")
    print("\n-- top 10 symbols --")
    for k, ts in sym_slices[-10:][::-1]:
        print(f"  {k:<22} {_agg(ts)}")

    status = "PASS" if result.passed else f"FAIL ({result.reject_reason})"
    print(f"\n{'-' * 78}")
    print(
        f"  ENGINE SCORE (legs): {status} | trades={result.total_trades} "
        f"WR={result.win_rate:.1%} PF={result.profit_factor:.2f} "
        f"E={result.expectancy_r:+.3f}R maxDD={result.max_drawdown_r:.1f}R"
    )
    print("  NOTE: metrics are PRE-COST (engine models no slippage/brokerage)")


async def diagnose(
    spec_path: Path,
    from_dt: datetime | None,
    to_dt: datetime | None,
    pool: asyncpg.Pool,
    json_out: Path | None,
    summary_only: bool = False,
    eval_from: datetime | None = None,
    cost_pct: float = 0.0,
    symbols: list[str] | None = None,
) -> None:
    strategy = compile_spec(spec_path)
    name = strategy.spec.meta.name
    tf = TF_MAP.get(str(strategy.spec.meta.timeframe), "1d")
    intraday = strategy.spec.intraday is not None
    engine = BacktestEngine(cost_pct=cost_pct)

    market_df = None
    if strategy.spec.market_filter is not None and strategy.spec.market_filter.enabled:
        async with pool.acquire() as conn:
            market_df = await fetch_df(conn, strategy.spec.market_filter.symbol, tf, from_dt, to_dt)
        if market_df is None and not summary_only:
            print(
                f"  WARNING: no benchmark data for {strategy.spec.market_filter.symbol} "
                f"({tf}) — market gate will fail closed on missing bars"
            )
        elif market_df is not None and not summary_only:
            print(
                f"  benchmark {strategy.spec.market_filter.symbol}: "
                f"{market_df.index.min()} -> {market_df.index.max()}"
            )

    trades: list[dict] = []
    all_signals = []
    no_data = 0
    async with pool.acquire() as conn:
        for sym in symbols or NIFTY50:
            df = await fetch_df(conn, sym, tf, from_dt, to_dt)
            if df is None:
                no_data += 1
                continue
            try:
                result = engine.run(strategy, df, market_df=market_df)
            except Exception as e:  # keep going; report symbol failures
                print(f"  {sym}: backtest error: {e}")
                continue
            for s in result.signals:
                if s.pnl_r is None:
                    continue
                # --eval-from: candles before it feed indicator warmup only;
                # trades entered before it are excluded from scoring.
                if eval_from is not None and pd.Timestamp(s.date) < eval_from:
                    continue
                ts_ist = pd.Timestamp(s.date).tz_convert(IST)
                trades.append(
                    {
                        "symbol": sym,
                        "entry_ts": str(ts_ist),
                        "direction": str(s.direction),
                        "exit_reason": s.exit_reason,
                        "pnl_r": float(s.pnl_r) - engine._cost_r(s),
                        "partial_r": None if s.partial_r is None else float(s.partial_r),
                        "tod": f"{ts_ist.hour:02d}:{ts_ist.minute // 30 * 30:02d}",
                        "dow": ts_ist.strftime("%a"),
                        "month": ts_ist.strftime("%Y-%m"),
                    }
                )
            all_signals.extend(
                s for s in result.signals if eval_from is None or pd.Timestamp(s.date) >= eval_from
            )

    score = engine._score(name, all_signals)
    window = f"{from_dt or 'begin'} -> {to_dt or 'end'}"
    if json_out:
        json_out.write_text(json.dumps({"strategy": name, "window": window, "trades": trades}))
    if summary_only:
        status = "PASS" if score.passed else "FAIL"
        print(
            f"{name:<28} trades={score.total_trades:<5} WR={score.win_rate:6.1%} "
            f"PF={min(score.profit_factor, 99):5.2f} E={score.expectancy_r:+.3f}R "
            f"maxDD={score.max_drawdown_r:6.1f}R  {status:<4} {score.reject_reason}"
        )
        return
    print(f"\n  spec={spec_path}  tf={tf}  window={window}  symbols_no_data={no_data}")
    print_report(name, trades, score, intraday)
    if json_out:
        print(f"  trades written to {json_out}")


def parse_date(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if s else None


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", action="append", help="strategy JSON path (repeatable)")
    ap.add_argument("--from", dest="from_", metavar="YYYY-MM-DD")
    ap.add_argument("--to", metavar="YYYY-MM-DD")
    ap.add_argument(
        "--eval-from",
        dest="eval_from",
        metavar="YYYY-MM-DD",
        help="score only trades entered on/after this date "
        "(earlier candles still feed indicator warmup)",
    )
    ap.add_argument(
        "--cost-pct",
        dest="cost_pct",
        type=float,
        default=0.0,
        help="round-trip transaction cost as %% of notional "
        "(converted to R per trade via its stop distance)",
    )
    ap.add_argument(
        "--universe",
        choices=["nifty50", "all"],
        default="nifty50",
        help="'all' = every non-index symbol with data in the DB",
    )
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--summary", action="store_true", help="one-line engine score per spec")
    args = ap.parse_args()

    if args.spec:
        specs = [Path(s) for s in args.spec]
    else:
        reg = json.loads((ROOT / "config/strategies_registry.json").read_text())
        specs = [ROOT / e["spec_path"] for e in reg["strategies"] if e.get("enabled")]

    pool = await asyncpg.create_pool(db_url(), min_size=1, max_size=4)
    try:
        symbols = None
        if args.universe == "all":
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT symbol FROM ohlcv_candles "
                    "WHERE timeframe='1d' AND symbol NOT LIKE '%% %%' ORDER BY symbol"
                )
            symbols = [r["symbol"] for r in rows]
            print(f"universe=all: {len(symbols)} symbols")
        for spec_path in specs:
            await diagnose(
                spec_path,
                parse_date(args.from_),
                parse_date(args.to),
                pool,
                args.json_out if len(specs) == 1 else None,
                summary_only=args.summary,
                eval_from=parse_date(args.eval_from),
                cost_pct=args.cost_pct,
                symbols=symbols,
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
