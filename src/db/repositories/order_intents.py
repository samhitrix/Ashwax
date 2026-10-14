"""OrderIntentRepository — durable ledger behind the idempotency guard (migration 072)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class OrderIntentRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def begin(
        self,
        key: str,
        *,
        agent_trade_id: str,
        signal_id: str,
        symbol: str,
        direction: str,
        quantity: int,
        price: float | None,
        is_paper: bool,
        window_seconds: float,
    ) -> tuple[bool, str | None, str | None]:
        """Atomically claim ``key``. Returns ``(claimed, existing_state, existing_order_id)``.

        Claimed when the key is new, when the previous attempt FAILED, or when the previous
        attempt is older than ``window_seconds`` (a genuine later retry, e.g. the AMO retry
        5 minutes apart). Not claimed = an attempt with the same key is PENDING/SUBMITTED
        inside the window, i.e. a duplicate.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO order_intents (
                    intent_key, agent_trade_id, signal_id, symbol, direction, quantity,
                    price, is_paper, state
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'PENDING')
                ON CONFLICT (intent_key) DO UPDATE SET
                    agent_trade_id = EXCLUDED.agent_trade_id,
                    state = 'PENDING', broker_order_id = NULL, updated_at = NOW()
                WHERE order_intents.state = 'FAILED'
                   OR order_intents.updated_at < NOW() - make_interval(secs => $9)
                RETURNING intent_key
                """,
                key,
                agent_trade_id,
                signal_id,
                symbol,
                direction,
                quantity,
                price,
                is_paper,
                float(window_seconds),
            )
            if row is not None:
                return True, None, None
            existing = await conn.fetchrow(
                "SELECT state, broker_order_id FROM order_intents WHERE intent_key = $1", key
            )
        if existing is None:  # raced with a purge; treat as claimable
            return True, None, None
        return False, str(existing["state"]), existing["broker_order_id"]

    async def mark_submitted(self, key: str, broker_order_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE order_intents SET state = 'SUBMITTED', broker_order_id = $2, "
                "updated_at = NOW() WHERE intent_key = $1",
                key,
                broker_order_id,
            )

    async def mark_failed(self, key: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE order_intents SET state = 'FAILED', updated_at = NOW() "
                "WHERE intent_key = $1",
                key,
            )

    async def purge_older_than(self, days: int) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM order_intents WHERE updated_at < NOW() - make_interval(days => $1)",
                days,
            )
        return int(result.split()[-1])
