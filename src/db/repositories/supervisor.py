"""
SupervisorRepository — DB bus for the Runtime Supervisor.

Tables: runtime_events, engine_heartbeats, runtime_incidents, system_control_flags.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg


class SupervisorRepository:
    """Data access for supervisor tables."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ── runtime_events ────────────────────────────────────────────────────────

    async def emit_event(
        self,
        event_type: str,
        source: str,
        severity: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert a runtime event. Returns the new row id."""
        import json

        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO runtime_events (event_type, source, severity, payload)
                    VALUES ($1, $2, $3, $4::jsonb)
                    RETURNING id
                    """,
                    event_type,
                    source,
                    severity,
                    json.dumps(payload) if payload else None,
                )
            )

    async def recent_events(
        self, limit: int = 50, severity: str | None = None
    ) -> list[asyncpg.Record]:
        """Fetch recent events, optionally filtered by severity."""
        async with self._pool.acquire() as conn:
            if severity:
                return list(
                    await conn.fetch(
                        "SELECT * FROM runtime_events WHERE severity = $1"
                        " ORDER BY created_at DESC LIMIT $2",
                        severity,
                        limit,
                    )
                )
            return list(
                await conn.fetch(
                    "SELECT * FROM runtime_events ORDER BY created_at DESC LIMIT $1",
                    limit,
                )
            )

    # ── engine_heartbeats ─────────────────────────────────────────────────────

    async def beat(
        self,
        engine_name: str,
        pid: int | None = None,
        status: str = "ALIVE",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Upsert a heartbeat row for the given engine."""
        import json

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO engine_heartbeats (engine_name, pid, status, metadata, last_beat)
                VALUES ($1, $2, $3, $4::jsonb, NOW())
                ON CONFLICT (engine_name) DO UPDATE
                    SET pid = EXCLUDED.pid,
                        status = EXCLUDED.status,
                        metadata = EXCLUDED.metadata,
                        last_beat = NOW()
                """,
                engine_name,
                pid,
                status,
                json.dumps(metadata) if metadata else None,
            )

    async def all_heartbeats(self) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(await conn.fetch("SELECT * FROM engine_heartbeats"))

    # ── runtime_incidents ─────────────────────────────────────────────────────

    async def open_incident(
        self,
        dedup_key: str,
        invariant_id: str,
        severity: str,
        message: str,
        remediation_hint: str = "",
    ) -> int:
        """Open a new incident. Raises UniqueViolationError if one is already OPEN."""
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    """
                    INSERT INTO runtime_incidents
                        (dedup_key, invariant_id, severity, message, remediation_hint)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    dedup_key,
                    invariant_id,
                    severity,
                    message,
                    remediation_hint,
                )
            )

    async def resolve_incident(self, dedup_key: str, auto_fix: str | None = None) -> None:
        """Mark the OPEN incident for dedup_key as RESOLVED."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE runtime_incidents
                   SET lifecycle = 'RESOLVED',
                       resolved_at = NOW(),
                       updated_at = NOW(),
                       auto_fix_applied = COALESCE($2, auto_fix_applied)
                 WHERE dedup_key = $1 AND lifecycle = 'OPEN'
                """,
                dedup_key,
                auto_fix,
            )

    async def open_incidents(self) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return list(
                await conn.fetch(
                    "SELECT * FROM runtime_incidents WHERE lifecycle = 'OPEN'"
                    " ORDER BY created_at DESC"
                )
            )

    # ── system_control_flags ──────────────────────────────────────────────────

    async def get_flag(self, flag_name: str) -> bool:
        """Read a control flag. Raises KeyError if the flag does not exist."""
        async with self._pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT flag_value FROM system_control_flags WHERE flag_name = $1",
                flag_name,
            )
        if val is None:
            raise KeyError(f"Control flag not found: {flag_name!r}")
        return bool(val)

    async def set_flag(self, flag_name: str, value: bool, updated_by: str = "system") -> None:
        """Set a control flag. Does NOT create new flags."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE system_control_flags
                   SET flag_value = $2, updated_at = NOW(), updated_by = $3
                 WHERE flag_name = $1
                """,
                flag_name,
                value,
                updated_by,
            )
        if result == "UPDATE 0":
            raise KeyError(f"Control flag not found: {flag_name!r}")

    async def master_switch_on(self) -> bool:
        return await self.get_flag("master_switch")

    async def is_paper_mode(self) -> bool:
        return await self.get_flag("paper_mode")
