"""
Fundamentals validation backtest — retrospective join, not a walk-forward sim.

Per CLAUDE.md ("avoid speculative or unvalidated trading logic", "all
strategies must be validated... before deployment to live"): fundamentals_shadow
(src/market_data/fundamentals_fetcher.py, config/global.json fundamentals.enabled)
is log-only on every DSL-strategy signal — this script is the ONLY thing that
may ever conclude a fundamentals factor is real, and even then it only prints
a report for a human to review; nothing in this codebase reads its output
automatically to change CRM/G9 behavior.

Joins fundamentals_snapshot (migration 064) against closed trades/paper_trades
(nearest snapshot_date <= entry date, same symbol) and tests 3 hypotheses by
splitting closed trades into two buckets and comparing mean pnl_r (R-multiples):

  1. earnings_growth > 0          vs  <= 0 / NULL
  2. trailing_pe below sector median (yahoo_sector)  vs  above
  3. debt_to_equity bottom quartile  vs  top quartile

A hypothesis is only reported "signal is real" when BOTH:
  - >= 30 closed trades in EACH bucket (else "insufficient sample — inconclusive")
  - Welch's t-test on pnl_r gives p < 0.05 in the full range, AND the same
    sign of effect holds when the date range is split into two halves and
    tested independently (out-of-sample check) — a pooled-only significant
    result is reported "not out-of-sample stable", not "real".

Usage:
    python scripts/backtest_fundamentals_filter.py --from 2024-01-01 --to 2026-09-01
    python scripts/backtest_fundamentals_filter.py --from 2024-01-01 --to 2026-09-01 --json-out out.json
"""

from __future__ import annotations

# Standalone dev/backtest script: imports follow sys.path bootstrap (E402).
# ruff: noqa: E402
# mypy: ignore-errors
import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import asyncpg

MIN_BUCKET_SIZE = 30
P_VALUE_THRESHOLD = 0.05


def db_url() -> str:
    if os.environ.get("DB_URL"):
        return os.environ["DB_URL"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DB_URL="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DB_URL not found in environment or .env")


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: date
    pnl_r: float


@dataclass
class HypothesisResult:
    name: str
    bucket_a_label: str
    bucket_b_label: str
    n_a: int
    n_b: int
    mean_r_a: float
    mean_r_b: float
    p_value: float | None
    out_of_sample_stable: bool | None
    verdict: str


async def fetch_closed_trades(pool: asyncpg.Pool, from_dt: date, to_dt: date) -> list[ClosedTrade]:
    rows = await pool.fetch(
        """
        SELECT symbol, entry_timestamp, exit_timestamp, pnl_realized, initial_sl, fill_price, quantity
          FROM paper_trades
         WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
           AND exit_reason IS DISTINCT FROM 'manual_dup_entry_cleanup'
           AND entry_timestamp::date BETWEEN $1 AND $2
        UNION ALL
        SELECT symbol, entry_timestamp, exit_timestamp, pnl_realized, initial_sl, fill_price, quantity
          FROM trades
         WHERE order_state = 'CLOSED' AND exit_timestamp IS NOT NULL
           AND entry_timestamp::date BETWEEN $1 AND $2
        """,
        from_dt,
        to_dt,
    )
    out: list[ClosedTrade] = []
    for r in rows:
        fill = float(r["fill_price"] or 0.0)
        sl = float(r["initial_sl"] or 0.0)
        qty = int(r["quantity"] or 0)
        risk = abs(fill - sl) * qty or 1.0
        pnl_r = float(r["pnl_realized"] or 0.0) / risk
        out.append(
            ClosedTrade(symbol=r["symbol"], entry_date=r["entry_timestamp"].date(), pnl_r=pnl_r)
        )
    return out


async def fetch_fundamentals_asof(
    pool: asyncpg.Pool, symbol: str, as_of: date
) -> asyncpg.Record | None:
    """Nearest snapshot on or before `as_of` for one symbol."""
    return await pool.fetchrow(
        """
        SELECT * FROM fundamentals_snapshot
         WHERE symbol = $1 AND snapshot_date <= $2
         ORDER BY snapshot_date DESC LIMIT 1
        """,
        symbol,
        as_of,
    )


def _welch_t_test_p_value(a: list[float], b: list[float]) -> float | None:
    """Welch's t-test p-value (two-sided), no scipy dependency — Welch-Satterthwaite
    df + a normal-approximation p-value is adequate at the sample sizes this
    script requires (n>=30/bucket)."""
    if len(a) < 2 or len(b) < 2:
        return None
    mean_a, mean_b = statistics.mean(a), statistics.mean(b)
    var_a, var_b = statistics.variance(a), statistics.variance(b)
    n_a, n_b = len(a), len(b)
    se = ((var_a / n_a) + (var_b / n_b)) ** 0.5
    if se == 0:
        return None
    t_stat = (mean_a - mean_b) / se
    # Normal approximation to the two-sided p-value (valid for n>=30/bucket,
    # which MIN_BUCKET_SIZE already enforces before this is ever called).
    from math import erf, sqrt

    z = abs(t_stat)
    p_one_sided = 0.5 * (1.0 - erf(z / sqrt(2)))
    return 2.0 * p_one_sided


def _evaluate_hypothesis(
    name: str,
    bucket_a: list[float],
    bucket_b: list[float],
    bucket_a_label: str,
    bucket_b_label: str,
    split_bucket_a: tuple[list[float], list[float]] | None = None,
    split_bucket_b: tuple[list[float], list[float]] | None = None,
) -> HypothesisResult:
    n_a, n_b = len(bucket_a), len(bucket_b)
    if n_a < MIN_BUCKET_SIZE or n_b < MIN_BUCKET_SIZE:
        return HypothesisResult(
            name,
            bucket_a_label,
            bucket_b_label,
            n_a,
            n_b,
            statistics.mean(bucket_a) if bucket_a else 0.0,
            statistics.mean(bucket_b) if bucket_b else 0.0,
            None,
            None,
            f"insufficient sample — inconclusive (need >= {MIN_BUCKET_SIZE}/bucket)",
        )

    p_value = _welch_t_test_p_value(bucket_a, bucket_b)
    mean_a, mean_b = statistics.mean(bucket_a), statistics.mean(bucket_b)
    significant = p_value is not None and p_value < P_VALUE_THRESHOLD

    out_of_sample_stable: bool | None = None
    if significant and split_bucket_a is not None and split_bucket_b is not None:
        first_a, second_a = split_bucket_a
        first_b, second_b = split_bucket_b
        sign_pooled = mean_a > mean_b
        stable = True
        for sa, sb in ((first_a, first_b), (second_a, second_b)):
            if len(sa) < 5 or len(sb) < 5:
                stable = False
                break
            if (statistics.mean(sa) > statistics.mean(sb)) != sign_pooled:
                stable = False
                break
        out_of_sample_stable = stable

    if not significant:
        verdict = f"not significant (p={p_value:.3f})" if p_value is not None else "not computable"
    elif out_of_sample_stable:
        verdict = f"SIGNAL LOOKS REAL (p={p_value:.3f}, out-of-sample stable) — still requires human review before any gate change"
    else:
        verdict = (
            f"significant pooled (p={p_value:.3f}) but NOT out-of-sample stable — treat as noise"
        )

    return HypothesisResult(
        name,
        bucket_a_label,
        bucket_b_label,
        n_a,
        n_b,
        mean_a,
        mean_b,
        p_value,
        out_of_sample_stable,
        verdict,
    )


async def run_backtest(pool: asyncpg.Pool, from_dt: date, to_dt: date) -> list[HypothesisResult]:
    trades = await fetch_closed_trades(pool, from_dt, to_dt)
    if not trades:
        return []

    # symbol -> as-of fundamentals row, one DB lookup per (symbol, entry_date) pair
    fund_cache: dict[tuple[str, date], asyncpg.Record | None] = {}
    for t in trades:
        key = (t.symbol, t.entry_date)
        if key not in fund_cache:
            fund_cache[key] = await fetch_fundamentals_asof(pool, t.symbol, t.entry_date)

    mid_date = from_dt + (to_dt - from_dt) / 2

    def _split(matched: list[ClosedTrade]) -> tuple[list[float], list[float]]:
        first = [t.pnl_r for t in matched if t.entry_date <= mid_date]
        second = [t.pnl_r for t in matched if t.entry_date > mid_date]
        return first, second

    # ── Hypothesis 1: earnings_growth > 0 vs <= 0 / NULL ──────────────────────
    h1_pos: list[ClosedTrade] = []
    h1_neg: list[ClosedTrade] = []
    for t in trades:
        row = fund_cache.get((t.symbol, t.entry_date))
        eg = row["earnings_growth"] if row else None
        (h1_pos if eg is not None and eg > 0 else h1_neg).append(t)

    # ── Hypothesis 2: trailing_pe below vs above same-day sector median ──────
    by_sector_pe: dict[str, list[float]] = {}
    pe_by_trade: dict[int, tuple[str, float] | None] = {}
    for i, t in enumerate(trades):
        row = fund_cache.get((t.symbol, t.entry_date))
        if row and row["trailing_pe"] is not None and row["yahoo_sector"]:
            sector = row["yahoo_sector"]
            pe = float(row["trailing_pe"])
            by_sector_pe.setdefault(sector, []).append(pe)
            pe_by_trade[i] = (sector, pe)
        else:
            pe_by_trade[i] = None
    sector_median_pe = {s: statistics.median(vals) for s, vals in by_sector_pe.items()}
    h2_below: list[ClosedTrade] = []
    h2_above: list[ClosedTrade] = []
    for i, t in enumerate(trades):
        info = pe_by_trade[i]
        if info is None:
            continue
        sector, pe = info
        (h2_below if pe < sector_median_pe[sector] else h2_above).append(t)

    # ── Hypothesis 3: debt_to_equity bottom vs top quartile ───────────────────
    dte_vals = [
        float(fund_cache[(t.symbol, t.entry_date)]["debt_to_equity"])
        for t in trades
        if fund_cache.get((t.symbol, t.entry_date))
        and fund_cache[(t.symbol, t.entry_date)]["debt_to_equity"] is not None
    ]
    h3_low: list[ClosedTrade] = []
    h3_high: list[ClosedTrade] = []
    if len(dte_vals) >= 8:
        sorted_vals = sorted(dte_vals)
        q1 = sorted_vals[len(sorted_vals) // 4]
        q3 = sorted_vals[(3 * len(sorted_vals)) // 4]
        for t in trades:
            row = fund_cache.get((t.symbol, t.entry_date))
            dte = (
                float(row["debt_to_equity"]) if row and row["debt_to_equity"] is not None else None
            )
            if dte is None:
                continue
            if dte <= q1:
                h3_low.append(t)
            elif dte >= q3:
                h3_high.append(t)

    return [
        _evaluate_hypothesis(
            "earnings_growth > 0 vs <= 0/NULL",
            [t.pnl_r for t in h1_pos],
            [t.pnl_r for t in h1_neg],
            "earnings_growth > 0",
            "earnings_growth <= 0 / NULL",
            _split(h1_pos),
            _split(h1_neg),
        ),
        _evaluate_hypothesis(
            "trailing_pe below vs above sector median",
            [t.pnl_r for t in h2_below],
            [t.pnl_r for t in h2_above],
            "PE < sector median",
            "PE >= sector median",
            _split(h2_below),
            _split(h2_above),
        ),
        _evaluate_hypothesis(
            "debt_to_equity bottom vs top quartile",
            [t.pnl_r for t in h3_low],
            [t.pnl_r for t in h3_high],
            "D/E bottom quartile",
            "D/E top quartile",
            _split(h3_low),
            _split(h3_high),
        ),
    ]


def print_report(results: list[HypothesisResult], n_trades: int) -> None:
    print(
        f"\n{'='*78}\nFundamentals validation backtest — {n_trades} closed trades matched\n{'='*78}"
    )
    if not results:
        print("No closed trades with matching fundamentals_snapshot rows in this range.")
        return
    for r in results:
        print(f"\n[{r.name}]")
        print(f"  {r.bucket_a_label}: n={r.n_a} mean_pnl_r={r.mean_r_a:.4f}")
        print(f"  {r.bucket_b_label}: n={r.n_b} mean_pnl_r={r.mean_r_b:.4f}")
        print(f"  verdict: {r.verdict}")
    print(
        f"\n{'='*78}\nDISCLAIMER: this output only informs a human decision. Per CLAUDE.md, "
        "no fundamentals signal from this script is ever automatically wired into "
        "CRM/G9 — that would require a separate, explicitly reviewed code change.\n"
        f"{'='*78}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="from_", required=True, metavar="YYYY-MM-DD")
    ap.add_argument("--to", required=True, metavar="YYYY-MM-DD")
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    from_dt = parse_date(args.from_)
    to_dt = parse_date(args.to)

    pool = await asyncpg.create_pool(db_url(), min_size=1, max_size=4)
    try:
        results = await run_backtest(pool, from_dt, to_dt)
        trades = await fetch_closed_trades(pool, from_dt, to_dt)
    finally:
        await pool.close()

    print_report(results, len(trades))
    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {"from": args.from_, "to": args.to, "results": [vars(r) for r in results]},
                default=str,
                indent=2,
            )
        )
        print(f"\n  full report written to {args.json_out}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
