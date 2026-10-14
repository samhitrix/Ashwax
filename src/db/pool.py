"""
asyncpg connection pool — module-level singleton.

Pattern: call init_pool() once at startup (orchestrator boot),
then get_pool() anywhere without passing the pool around.
Repositories accept the pool via constructor injection.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool(
    dsn: str | None = None,
    min_size: int = 4,
    max_size: int = 20,
) -> asyncpg.Pool:
    """
    Create the global asyncpg pool.

    Args:
        dsn: Postgres connection string. Falls back to DB_URL env var.
        min_size: minimum idle connections.
        max_size: maximum connections. Raised from 10 to 20 (2026-08-12): with
            ~15 concurrent orchestrator loops (regime, swing scan, intraday scan,
            health scoring, RS ranking, options, news, strategy reload, order
            monitor, supervisor invariants, ...) all sharing one pool, several
            loops firing in the same window could exhaust 10 connections and
            starve the supervisor's own "SELECT 1" liveness check — logged as
            INVARIANT INV_C8 CRITICAL: DB unreachable (empty message = the
            acquire itself timed out, not a real DB outage).

    Returns:
        The created pool (also accessible via get_pool()).
    """
    import asyncpg  # noqa: PLC0415

    global _pool

    if _pool is not None:
        return _pool

    url = dsn or os.environ.get("DB_URL")
    if not url:
        raise RuntimeError("DB_URL environment variable not set and no dsn argument provided.")

    _pool = await asyncpg.create_pool(
        url,
        min_size=min_size,
        max_size=max_size,
        command_timeout=30,
    )
    return _pool


def get_pool() -> asyncpg.Pool:
    """
    Return the global pool.  Raises RuntimeError if init_pool() was never called.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialised. Call await init_pool() before get_pool().")
    return _pool


async def close_pool() -> None:
    """Gracefully close the pool (called at orchestrator shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
