"""
DB package — asyncpg connection pool + repository layer.

Usage:
    from src.db import get_pool, init_pool, close_pool

    await init_pool()
    pool = get_pool()
    # use pool across repositories
    await close_pool()
"""

from src.db.pool import close_pool, get_pool, init_pool

__all__ = ["init_pool", "get_pool", "close_pool"]
