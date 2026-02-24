# src/common/database.py

"""
Database engine, session factory, and declarative Base for SQLAlchemy 2.0 + asyncpg.

Usage in routers / services:
    from src.common.dependencies import DbSession

    async def my_endpoint(db: DbSession) -> ...
"""

from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Engine – created once, reused for the lifetime of the process
# ---------------------------------------------------------------------------

def _build_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine from application settings."""
    url = settings.DATABASE_URL
    logger.info(f"Creating database engine with URL: {url}")

    # asyncpg requires the postgresql+asyncpg:// scheme
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    return create_async_engine(
        url,
        # --- connection pool ---
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,          # discard stale connections
        # --- misc ---
        echo=settings.DEBUG,         # SQL echo only in debug mode
        future=True,                 # SQLAlchemy 2.0 style
    )


engine: AsyncEngine = _build_engine()

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # avoid implicit I/O after commit
    autoflush=False,
    autocommit=False,
)

# ---------------------------------------------------------------------------
# Declarative Base – import this in your ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base."""
    pass

# ---------------------------------------------------------------------------
# Lifecycle helpers – called from main.py lifespan
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Verify the database is reachable on startup."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("Database connection pool initialised (%s)", settings.DATABASE_URL)


async def close_db() -> None:
    """Dispose the engine / connection pool on shutdown."""
    await engine.dispose()
    logger.info("Database connection pool closed.")

# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession; roll back on error, always close."""
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
