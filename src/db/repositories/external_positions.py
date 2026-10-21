"""ExternalPositionRepository — broker positions this agent did not open (migration 074)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

_CLASSIFICATIONS = frozenset({"UNCLASSIFIED", "MANUAL", "CASH_PARKING", "LEGACY", "OTHER"})


@dataclass
class ExternalSync:
    new: list[tuple[str, str]]  # (symbol, product) first detected this cycle
    closed: list[tuple[str, str]]  # (symbol, product) no longer held at the broker
    acknowledged: set[tuple[str, str]]  # open + acknowledged -> not a reconciliation gap


class ExternalPositionRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def sync(
        self, orphans: list[tuple[str, str, int, float]], exec_broker: str | None = None
    ) -> ExternalSync:
        """Reconcile the table with the CURRENT orphan set ``[(symbol, product, qty, avg)]``
        in one transaction: upsert each orphan (bump last_seen/times_seen, detect first
        sight), close OPEN rows no longer present, and report which open rows are
        acknowledged.

        ``exec_broker`` (per-broker reconciliation, migrations 089/090): only that broker's
        rows are read, updated, inserted (stamped with it) and closed, so one broker's
        reconcile never closes another's orphans; NULL rows belong to "kite". None = every
        row, the legacy single-broker behaviour (identical SQL)."""
        if exec_broker is not None:
            return await self._sync_broker(orphans, exec_broker)
        new: list[tuple[str, str]] = []
        closed: list[tuple[str, str]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            open_rows = await conn.fetch(
                "SELECT symbol, product, acknowledged FROM external_positions WHERE status = 'OPEN'"
            )
            known = {(r["symbol"], r["product"]): bool(r["acknowledged"]) for r in open_rows}
            current = {(s, p) for s, p, _q, _a in orphans}
            for symbol, product, qty, avg in orphans:
                if (symbol, product) in known:
                    await conn.execute(
                        "UPDATE external_positions SET quantity = $3, avg_price = $4, "
                        "last_seen_at = NOW(), times_seen = times_seen + 1 "
                        "WHERE symbol = $1 AND product = $2 AND status = 'OPEN'",
                        symbol,
                        product,
                        qty,
                        avg,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO external_positions (symbol, product, quantity, avg_price) "
                        "VALUES ($1, $2, $3, $4)",
                        symbol,
                        product,
                        qty,
                        avg,
                    )
                    new.append((symbol, product))
            for key in set(known) - current:
                await conn.execute(
                    "UPDATE external_positions SET status = 'CLOSED', closed_at = NOW() "
                    "WHERE symbol = $1 AND product = $2 AND status = 'OPEN'",
                    *key,
                )
                closed.append(key)
        acknowledged = {k for k, ack in known.items() if ack and k in current}
        return ExternalSync(new=new, closed=closed, acknowledged=acknowledged)

    async def _sync_broker(
        self, orphans: list[tuple[str, str, int, float]], exec_broker: str
    ) -> ExternalSync:
        """sync() scoped to one broker's rows (see sync)."""
        new: list[tuple[str, str]] = []
        closed: list[tuple[str, str]] = []
        async with self._pool.acquire() as conn, conn.transaction():
            open_rows = await conn.fetch(
                "SELECT symbol, product, acknowledged FROM external_positions "
                "WHERE status = 'OPEN' AND COALESCE(exec_broker, 'kite') = $1",
                exec_broker,
            )
            known = {(r["symbol"], r["product"]): bool(r["acknowledged"]) for r in open_rows}
            current = {(s, p) for s, p, _q, _a in orphans}
            for symbol, product, qty, avg in orphans:
                if (symbol, product) in known:
                    await conn.execute(
                        "UPDATE external_positions SET quantity = $3, avg_price = $4, "
                        "last_seen_at = NOW(), times_seen = times_seen + 1 "
                        "WHERE symbol = $1 AND product = $2 AND status = 'OPEN' "
                        "AND COALESCE(exec_broker, 'kite') = $5",
                        symbol,
                        product,
                        qty,
                        avg,
                        exec_broker,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO external_positions "
                        "(symbol, product, quantity, avg_price, exec_broker) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        symbol,
                        product,
                        qty,
                        avg,
                        exec_broker,
                    )
                    new.append((symbol, product))
            for key in set(known) - current:
                await conn.execute(
                    "UPDATE external_positions SET status = 'CLOSED', closed_at = NOW() "
                    "WHERE symbol = $1 AND product = $2 AND status = 'OPEN' "
                    "AND COALESCE(exec_broker, 'kite') = $3",
                    *key,
                    exec_broker,
                )
                closed.append(key)
        acknowledged = {k for k, ack in known.items() if ack and k in current}
        return ExternalSync(new=new, closed=closed, acknowledged=acknowledged)

    async def list_open(self) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, symbol, product, quantity, avg_price, classification, acknowledged, "
                "note, first_seen_at, last_seen_at, times_seen FROM external_positions "
                "WHERE status = 'OPEN' ORDER BY first_seen_at"
            )
        return [dict(r) for r in rows]

    async def acknowledge(self, position_id: int, classification: str, note: str | None) -> bool:
        if classification not in _CLASSIFICATIONS or classification == "UNCLASSIFIED":
            raise ValueError(
                f"classification must be one of {sorted(_CLASSIFICATIONS - {'UNCLASSIFIED'})}"
            )
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE external_positions SET acknowledged = TRUE, classification = $2, "
                "note = $3 WHERE id = $1 AND status = 'OPEN'",
                position_id,
                classification,
                note,
            )
        return bool(result.endswith(" 1"))
