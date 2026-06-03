"""
SQLAlchemy 2.x async database engine and session factory.

Engineering Specification references:
  Part 1, Section 1.2, Decision 6 — PgBouncer for production connection pooling;
                                     SQLAlchemy pool for development.
  Part 1, Section 2.3              — init_db() called at app startup via lifespan.

Milestone: M1-Step12 (session factory) + M1-Step10 (lifespan integration)
Status:    COMPLETE
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """SQLAlchemy declarative base — all ORM models inherit from this."""


# ---------------------------------------------------------------------------
# Module-level singletons — initialised once in the lifespan, then read-only
# ---------------------------------------------------------------------------

engine: AsyncEngine | None = None
AsyncSessionFactory: async_sessionmaker[AsyncSession] | None = None


def init_db(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 20,
    echo: bool = False,
) -> None:
    """
    Initialise the async engine and session factory.

    Called once during application startup (lifespan context manager in
    ``apps.api.main``). Subsequent calls are a no-op if the engine is
    already initialised — safe to call from test fixtures.

    Args:
        database_url:  asyncpg-compatible PostgreSQL URL
                       (postgresql+asyncpg://user:pass@host/db).
        pool_size:     Number of persistent connections in the pool.
                       Matches ``Settings.database_pool_size`` (default 10).
        max_overflow:  Extra connections allowed above pool_size under load.
                       Matches ``Settings.database_max_overflow`` (default 20).
        echo:          Set True in development to log all SQL statements.
                       Matches ``Settings.debug``.
    """
    global engine, AsyncSessionFactory
    if engine is not None:
        return  # already initialised — idempotent

    engine = create_async_engine(
        database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # validate connections before use (handles stale conns)
    )
    AsyncSessionFactory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def dispose_db() -> None:
    """
    Close all pooled connections and release the engine.

    Called during application shutdown (lifespan context manager).
    Gracefully drains in-progress queries before closing.
    """
    global engine, AsyncSessionFactory
    if engine is not None:
        await engine.dispose()
        engine = None
        AsyncSessionFactory = None


async def check_db() -> bool:
    """
    Verify the database is reachable with a lightweight ping query.

    Used by the ``/health/ready`` readiness probe.

    Returns:
        True if the database is healthy, False otherwise.
    """
    if engine is None:
        return False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields one ``AsyncSession`` per HTTP request.

    Commits automatically on success; rolls back on exception.
    The session is always closed in the ``finally`` block.

    Raises:
        RuntimeError: If ``init_db()`` has not been called (startup incomplete).
    """
    if AsyncSessionFactory is None:
        raise RuntimeError(
            "Database not initialised. "
            "Ensure init_db() is called in the application lifespan."
        )
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
