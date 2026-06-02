"""
SQLAlchemy 2.x async database engine and session factory.

Milestone: M1-Step12
"""
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# TODO M1: import get_settings and use settings.database_url


class Base(DeclarativeBase):
    """SQLAlchemy declarative base — all ORM models inherit from this."""
    pass


# Engine and session factory (initialised in app lifespan — TODO M1-Step10)
engine = None
AsyncSessionFactory: async_sessionmaker | None = None


def init_db(database_url: str) -> None:
    """Initialise the async engine and session factory. Called at app startup."""
    global engine, AsyncSessionFactory
    engine = create_async_engine(
        database_url,
        echo=False,        # TODO M1: set echo=settings.debug
        pool_size=10,      # TODO M1: load from settings
        max_overflow=20,
    )
    AsyncSessionFactory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields a database session per request."""
    if AsyncSessionFactory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
