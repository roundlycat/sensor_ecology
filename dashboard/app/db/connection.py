"""
Async Postgres connection pool using asyncpg.
Call init_pool() at startup and close_pool() at shutdown.
Use get_pool() anywhere else to acquire a connection.
"""

import asyncpg

from app.config import DB_DSN

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized — was init_pool() called?")
    return _pool
