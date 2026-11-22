"""NotificationRepository — notifications_sent.

Audit log + idempotency ledger for the Telegram notifier (src/notifications/).
Not a queue: the notifier is an in-process asyncio task inside the
orchestrator. record() is best-effort — a DB hiccup must never prevent (or
retry) a notification that Telegram already accepted or rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class NotificationRepository:
    """Data access for the notifications_sent table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(
        self,
        *,
        kind: str,
        body: str,
        is_paper: bool,
        agent_trade_id: str | None = None,
        symbol: str | None = None,
        dedupe_key: str | None = None,
        status: str = "SENT",
        error: str | None = None,
        attempts: int = 1,
    ) -> bool:
        """Insert an audit row. Returns False (no insert) if dedupe_key already
        exists — i.e. this notification was already recorded as sent/attempted.
        Never raises: logged and swallowed on failure."""
        import logging

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO notifications_sent
                        (kind, agent_trade_id, symbol, is_paper, dedupe_key,
                         body, status, error, attempts)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
                    RETURNING id
                    """,
                    kind,
                    agent_trade_id,
                    symbol,
                    is_paper,
                    dedupe_key,
                    body,
                    status,
                    error,
                    attempts,
                )
                return row is not None
        except Exception as exc:
            logging.getLogger(__name__).warning(  # "src.db.repositories.notifications" -> db.log
                "notification_record_failed kind=%s err=%s", kind, exc
            )
            # A silent audit-table failure is how notifications_sent stayed empty for weeks (the
            # ON CONFLICT target did not match the partial unique index). Surface it -- Alerts tab
            # only (push=False: this table is fed by the notifier itself, so pushing would recurse).
            try:
                from src.infrastructure import operator_alerts

                operator_alerts.raise_alert(
                    f"notifications_sent INSERT is failing ({exc}) -- the Telegram audit log is "
                    "not being saved",
                    source="db:notifications_sent",
                    dedupe_seconds=3600,
                    push=False,
                )
            except Exception:  # noqa: BLE001
                pass
            return False

    async def already_sent(self, dedupe_key: str) -> bool:
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notifications_sent WHERE dedupe_key = $1 AND status = 'SENT'",
                    dedupe_key,
                )
                return row is not None
        except Exception:
            return False

    async def recent(self, limit: int = 100) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM notifications_sent ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            )
