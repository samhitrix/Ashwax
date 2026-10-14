"""
Unit tests for src/db/migrate.py

All DB calls are mocked — no real Postgres required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.db.migrate import _VERSION_RE, _migration_files, pending_count, run_migrations

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_pool(applied_versions: list[str] | None = None) -> MagicMock:
    """Build a mock asyncpg Pool returning the given applied versions."""
    applied = applied_versions or []

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[{"version": v} for v in applied])
    conn.fetchval = AsyncMock(return_value=None)

    # transaction() returns an async context manager
    tx_ctx = AsyncMock()
    tx_ctx.__aenter__ = AsyncMock(return_value=None)
    tx_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_ctx)

    pool = MagicMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


# ── _migration_files ──────────────────────────────────────────────────────────


def test_migration_files_sorted() -> None:
    files = _migration_files()
    assert len(files) > 0
    versions = [v for v, _ in files]
    assert versions == sorted(versions)


def test_migration_files_names_match_pattern() -> None:
    for _, path in _migration_files():
        assert _VERSION_RE.match(path.name), f"Bad filename: {path.name}"


def test_migration_files_all_exist() -> None:
    for _, path in _migration_files():
        assert path.exists(), f"Missing: {path}"


# ── run_migrations ────────────────────────────────────────────────────────────


async def test_run_migrations_applies_all_when_none_applied() -> None:
    pool = _make_pool(applied_versions=[])
    count = await run_migrations(pool)
    assert count == len(_migration_files())


async def test_run_migrations_skips_already_applied() -> None:
    all_files = _migration_files()
    first_version = all_files[0][0]
    pool = _make_pool(applied_versions=[first_version])
    count = await run_migrations(pool)
    assert count == len(all_files) - 1


async def test_run_migrations_idempotent_when_all_applied() -> None:
    all_versions = [v for v, _ in _migration_files()]
    pool = _make_pool(applied_versions=all_versions)
    count = await run_migrations(pool)
    assert count == 0


async def test_run_migrations_raises_on_db_error() -> None:
    pool = _make_pool(applied_versions=[])
    conn = pool.acquire.return_value.__aenter__.return_value

    call_count = 0

    async def failing_execute(sql: str, *args: object) -> None:
        nonlocal call_count
        call_count += 1
        # 1st = CREATE TABLE schema_migrations (allowed)
        # 2nd = actual migration DDL (fail it)
        if call_count >= 2:
            raise Exception("syntax error")

    conn.execute = failing_execute

    with pytest.raises(RuntimeError, match="failed"):
        await run_migrations(pool)


# ── pending_count ─────────────────────────────────────────────────────────────


async def test_pending_count_all_pending() -> None:
    pool = _make_pool(applied_versions=[])
    count = await pending_count(pool)
    assert count == len(_migration_files())


async def test_pending_count_zero() -> None:
    all_versions = [v for v, _ in _migration_files()]
    pool = _make_pool(applied_versions=all_versions)
    count = await pending_count(pool)
    assert count == 0


async def test_pending_count_partial() -> None:
    all_files = _migration_files()
    half = len(all_files) // 2
    applied = [v for v, _ in all_files[:half]]
    pool = _make_pool(applied_versions=applied)
    count = await pending_count(pool)
    assert count == len(all_files) - half
