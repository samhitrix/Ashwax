"""One-off: add partial_pnl_booked back into pnl_realized for trades closed BEFORE the
2026-09-24 fix (pnl_realized then held only the last leg; the banked partial sat in
partial_pnl_booked and was in no total).

Dry-run by default. Bound the window explicitly so it can never double-apply: rows closed
by the fixed code already include the partial. Use --before = the moment the fixed build
went live (service restart); if trades were closed by the old build after a previous run,
run again with --after <previous --before> --before <restart time>.

    python scripts/backfill_partial_pnl.py --before 2026-09-24T14:00:00+05:30
    python scripts/backfill_partial_pnl.py --before 2026-09-24T14:00:00+05:30 --apply
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from src.db.pool import init_pool  # noqa: E402

_SELECT = """
SELECT agent_trade_id, symbol, strategy_name, quantity, pnl_realized, partial_pnl_booked,
       exit_timestamp
  FROM {t}
 WHERE order_state = 'CLOSED' AND partial_pnl_booked <> 0
   AND exit_timestamp < $1 AND ($2::timestamptz IS NULL OR exit_timestamp >= $2)
 ORDER BY exit_timestamp
"""
_UPDATE = """
UPDATE {t} SET pnl_realized = pnl_realized + partial_pnl_booked, updated_at = NOW()
 WHERE order_state = 'CLOSED' AND partial_pnl_booked <> 0
   AND exit_timestamp < $1 AND ($2::timestamptz IS NULL OR exit_timestamp >= $2)
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="ISO ts with offset; rows closed before it")
    ap.add_argument("--after", default=None, help="ISO ts with offset; rows closed at/after it")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    a = ap.parse_args()
    before = datetime.fromisoformat(a.before)
    after = datetime.fromisoformat(a.after) if a.after else None
    pool = await init_pool()
    async with pool.acquire() as conn, conn.transaction():
        for t in ("paper_trades", "trades"):
            rows = await conn.fetch(_SELECT.format(t=t), before, after)  # noqa: S608
            print(f"{t}: {len(rows)} closed trade(s) with a partial")
            for r in rows:
                new = float(r["pnl_realized"] or 0) + float(r["partial_pnl_booked"])
                print(
                    f"  {r['agent_trade_id']} {r['symbol']:<12} {r['strategy_name'] or '':<18} "
                    f"pnl_realized {float(r['pnl_realized'] or 0):>10.2f} + partial "
                    f"{float(r['partial_pnl_booked']):>9.2f} -> {new:>10.2f}"
                )
            if a.apply and rows:
                await conn.execute(_UPDATE.format(t=t), before, after)  # noqa: S608
        print("APPLIED" if a.apply else "DRY-RUN (nothing written; add --apply)")


if __name__ == "__main__":
    asyncio.run(main())
