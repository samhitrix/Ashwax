"""ReportingRepository — capital_reconciliation_log, corporate_action_log, tax_audit_trail."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class ReportingRepository:
    """Data access for reporting and audit tables."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def log_reconciliation(
        self,
        recon_date: date,
        db_capital: Decimal,
        broker_capital: Decimal,
        action_taken: str | None = None,
    ) -> int:
        drift = broker_capital - db_capital
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO capital_reconciliation_log
                        (recon_date, db_capital, broker_capital, drift_rupees, action_taken)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    recon_date,
                    db_capital,
                    broker_capital,
                    drift,
                    action_taken,
                )
            )

    async def recent_reconciliations(self, limit: int = 30) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM capital_reconciliation_log ORDER BY recon_date DESC LIMIT $1",
                    limit,
                )
            )

    async def log_corporate_action(
        self,
        symbol: str,
        action_type: str,
        action_date: date,
        ratio_or_amount: Decimal | None = None,
        trade_id: int | None = None,
        adjustment_applied: dict[str, Any] | None = None,
    ) -> int:
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO corporate_action_log
                        (symbol, action_type, action_date, ratio_or_amount,
                         trade_id, adjustment_applied)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    RETURNING id
                    """,
                    symbol,
                    action_type,
                    action_date,
                    ratio_or_amount,
                    trade_id,
                    json.dumps(adjustment_applied) if adjustment_applied else None,
                )
            )

    async def append_audit(
        self,
        event_type: str,
        event_data: dict[str, Any],
        trade_id: int | None = None,
        user_id: str | None = None,
    ) -> int:
        """
        Append an immutable audit record.

        Never UPDATE or DELETE tax_audit_trail — SEBI requires 8-year retention.
        """
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO tax_audit_trail
                        (event_type, event_data, trade_id, user_id)
                    VALUES ($1, $2::jsonb, $3, $4)
                    RETURNING id
                    """,
                    event_type,
                    json.dumps(event_data),
                    trade_id,
                    user_id,
                )
            )

    async def audit_for_trade(self, trade_id: int) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM tax_audit_trail WHERE trade_id = $1 ORDER BY created_at ASC",
                    trade_id,
                )
            )
