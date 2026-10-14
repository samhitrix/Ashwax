"""
Migration runner — applies pending SQL migrations in version order.

Tracks applied versions in `schema_migrations` table so reruns are idempotent.
Each migration file is applied in a single transaction; failure rolls back that
file and stops the runner (no partial schema).

Usage (one-shot at boot):
    from src.db.migrate import run_migrations
    await run_migrations(pool)
"""

from __future__ import annotations

import logging
import pathlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

_LOG = logging.getLogger("src.db.migrate")

_MIGRATIONS_DIR = pathlib.Path(__file__).parent.parent.parent / "migrations"

# Extract the version prefix from filenames like "001_core_tables.sql"
_VERSION_RE = re.compile(r"^(\d+)_")


def _migration_files() -> list[tuple[str, pathlib.Path]]:
    """Return (version, path) pairs sorted by version number."""
    pairs: list[tuple[str, pathlib.Path]] = []
    for p in _MIGRATIONS_DIR.glob("*.sql"):
        m = _VERSION_RE.match(p.name)
        if m:
            pairs.append((m.group(1).zfill(6), p))
    pairs.sort(key=lambda x: x[0])
    return pairs


async def _ensure_migrations_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT        PRIMARY KEY,
            name        TEXT        NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


async def _applied_versions(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def run_migrations(pool: asyncpg.Pool) -> int:
    """
    Apply all pending migrations.

    Args:
        pool: asyncpg connection pool.

    Returns:
        Number of migrations applied in this run (0 if already up-to-date).

    Raises:
        RuntimeError: if a migration file fails (schema left unchanged for that file).
    """
    all_files = _migration_files()
    if not all_files:
        _LOG.warning("No migration files found in %s", _MIGRATIONS_DIR)
        return 0

    applied_count = 0

    async with pool.acquire() as conn:
        await _ensure_migrations_table(conn)
        applied = await _applied_versions(conn)

        for version, path in all_files:
            if version in applied:
                continue

            sql = path.read_text(encoding="utf-8")
            _LOG.info("Applying migration %s (%s)", version, path.name)

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        version,
                        path.name,
                    )
            except Exception as exc:
                raise RuntimeError(f"Migration {path.name} failed: {exc}") from exc

            applied_count += 1
            _LOG.info("  OK — %s", path.name)

    if applied_count == 0:
        _LOG.info("Database schema is up-to-date (%d migrations)", len(all_files))
    else:
        _LOG.info("Applied %d migration(s)", applied_count)

    return applied_count


async def pending_count(pool: asyncpg.Pool) -> int:
    """Return how many migrations have not yet been applied."""
    all_files = _migration_files()
    async with pool.acquire() as conn:
        await _ensure_migrations_table(conn)
        applied = await _applied_versions(conn)
    return sum(1 for v, _ in all_files if v not in applied)


def main() -> None:
    """``python -m src.db.migrate`` — create/upgrade the schema in the DB named by
    DB_URL (loaded from .env). The orchestrator also runs this at startup; this
    entry point is for first-time setup and CI."""
    import asyncio  # noqa: PLC0415

    from dotenv import load_dotenv  # noqa: PLC0415

    from src.db.pool import close_pool, init_pool  # noqa: PLC0415

    async def _run() -> None:
        load_dotenv()
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        pool = await init_pool()
        try:
            await run_migrations(pool)
        finally:
            await close_pool()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
