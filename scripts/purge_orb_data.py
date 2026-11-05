"""One-off purge of every ORB-strategy row from the AshwaX Postgres DB.

The ORB family (orb_intraday, _smart, _top, _v2, _v3) was deleted as a strategy on
2026-09-11. This removes its leftover history. Dry-run by default; ``--apply``
backs the rows up to data/backups/ and then deletes them in ONE transaction.

Usage:
    python scripts/purge_orb_data.py            # dry run: print counts only
    python scripts/purge_orb_data.py --apply    # backup + delete + verify + commit
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "data" / "backups" / f"orb_cleanup_{date.today().isoformat()}.json"

ORB_PAPER = "SELECT {col} FROM paper_trades WHERE strategy_name ILIKE 'orb%'"

# (table, WHERE clause). Order matters: rows linked to paper_trades go first, because
# paper_trades is the link source and must still exist when they are matched.
DELETES: list[tuple[str, str]] = [
    ("trade_insights", f"trade_id IN ({ORB_PAPER.format(col='id')})"),
    ("order_monitor_breaches", f"agent_trade_id IN ({ORB_PAPER.format(col='agent_trade_id')})"),
    ("manual_order_actions", f"agent_trade_id IN ({ORB_PAPER.format(col='agent_trade_id')})"),
    ("paper_trades", "strategy_name ILIKE 'orb%'"),
    ("performance_attribution", "strategy_name ILIKE 'orb%'"),
    ("strategy_registry_db", "strategy_name ILIKE 'orb%'"),
    # strategy name is stored in a column called model_used
    ("llm_calibration_history", "model_used ILIKE 'orb%'"),
    ("runtime_incidents", "dedup_key LIKE 'strategy_decay_orb%'"),
    # Migration 083 ledger. MUST come after paper_trades: its DELETE trigger appends one
    # 'DELETED' ledger row per deleted paper trade (full old row in `detail`), so those rows
    # only exist once paper_trades has been deleted in this transaction.
    (
        "trade_executions",
        f"strategy ILIKE 'orb%' OR trade_id IN ({ORB_PAPER.format(col='agent_trade_id')})",
    ),
]
# jsonb array column: strip ORB elements, keep the row.
REGIME_WHERE = "avoid_strategies::text ~* 'orb'"

# Counts measured 2026-09-28. Live-written tables (LIVE) may drift; the rest must match.
EXPECTED = {
    "trade_insights": 409,
    "order_monitor_breaches": 14133,
    "manual_order_actions": 6,
    "paper_trades": 423,
    "performance_attribution": 375,
    "strategy_registry_db": 5,
    "llm_calibration_history": 13,
    "runtime_incidents": 2,
    "market_regime_log": 849,
    "trade_executions": 0,
}
LIVE = {"order_monitor_breaches", "market_regime_log", "trade_executions"}
STATIC = set(EXPECTED) - LIVE


def count(cur: Any, table: str, where: str) -> int:
    cur.execute(f"SELECT count(*) FROM {table} WHERE {where}")  # noqa: S608 -- table names are module constants
    return int(cur.fetchone()[0])


def backup(cur: Any) -> None:
    out = {}
    for table, where in DELETES:
        cur.execute(f"SELECT * FROM {table} WHERE {where}")  # noqa: S608 -- table names are module constants
        cols = [d[0] for d in cur.description]
        out[table] = {"columns": cols, "rows": cur.fetchall()}
    cur.execute(f"SELECT id, avoid_strategies FROM market_regime_log WHERE {REGIME_WHERE}")  # noqa: S608 -- table names are module constants
    out["market_regime_log"] = {"columns": ["id", "avoid_strategies"], "rows": cur.fetchall()}
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(out, default=str), encoding="utf-8")
    print(f"backup written: {BACKUP} ({BACKUP.stat().st_size:,} bytes)")


def residue(cur: Any) -> dict[str, int]:
    """Row-to-text ORB regex scan over every public table."""
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
    hits = {}
    for (t,) in cur.fetchall():
        cur.execute(
            f'SELECT count(*) FROM "{t}" x WHERE x::text ~* '  # noqa: S608 -- names come from pg_tables
            "'(^|[^a-z])orb([^a-z]|$)|orb_intraday|intraday_orb|orb_smart'"
        )
        if n := cur.fetchone()[0]:
            hits[t] = n
    return hits


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit the purge (default: dry run)")
    args = ap.parse_args()

    conn = psycopg2.connect(dotenv_values(ROOT / ".env")["DB_URL"])
    cur = conn.cursor()
    try:
        counts = {t: count(cur, t, w) for t, w in DELETES}
        counts["market_regime_log"] = count(cur, "market_regime_log", REGIME_WHERE)
        for t, n in counts.items():
            print(f"{t:26s} {n:>7,}  (expected {EXPECTED[t]:,})")

        bad = [t for t in STATIC if counts[t] != EXPECTED[t]]
        bad += [t for t in LIVE if counts[t] < EXPECTED[t]]
        if bad:
            print(f"ABORT: counts differ from the reviewed plan for {bad}")
            return 1
        if not args.apply:
            print("dry run only. re-run with --apply to purge.")
            return 0

        backup(cur)
        deleted = {}
        for t, w in DELETES:
            cur.execute(f"DELETE FROM {t} WHERE {w}")  # noqa: S608 -- table names are module constants
            deleted[t] = cur.rowcount
        cur.execute(
            "UPDATE market_regime_log SET avoid_strategies = COALESCE("  # noqa: S608 -- REGIME_WHERE is a module constant
            "(SELECT jsonb_agg(e) FROM jsonb_array_elements(avoid_strategies) e "
            "WHERE e #>> '{}' !~* '^orb'), '[]'::jsonb) "
            f"WHERE {REGIME_WHERE}"
        )
        deleted["market_regime_log"] = cur.rowcount

        for t, n in deleted.items():
            if n < counts[t] or (t in STATIC and n != counts[t]):
                raise RuntimeError(f"{t}: deleted/updated {n}, counted {counts[t]}")
        cur.execute("SELECT count(*) FROM paper_trades")
        paper_left = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM trades")
        trades_left = cur.fetchone()[0]
        if paper_left != 50 or trades_left != 5:
            raise RuntimeError(
                f"unexpected survivors: paper_trades={paper_left} trades={trades_left}"
            )

        left = residue(cur)
        print("residue before commit:", left)
        if set(left) - {"news_events"}:
            raise RuntimeError(f"ORB residue remains: {left}")
        conn.commit()
        print("COMMITTED", deleted)
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"ROLLED BACK: {exc}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
