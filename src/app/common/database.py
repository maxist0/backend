# src/app/common/database.py

"""
Database engine, session factory, and declarative Base for SQLAlchemy 2.0 + asyncpg.

Usage in routers / services:
    from app.common.dependencies import DbSession

    async def my_endpoint(db: DbSession) -> ...
"""

from typing import AsyncGenerator
from sqlalchemy.engine import make_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.common.models import Base  # noqa: F401 – registers metadata

from app.common.config import settings
from app.common.logging import get_logger

__all__ = ["get_db", "init_db", "close_db", "Base"]

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Engine – created once, reused for the lifetime of the process
# ---------------------------------------------------------------------------


def _build_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine from application settings."""
    url: str = settings.DATABASE_URL

    maked_url = make_url(url)  # validate and parse the URL
    logger.info(
        "db.engine_creating", url=maked_url.render_as_string(hide_password=True)
    )

    # asyncpg requires the postgresql+asyncpg:// scheme
    # if url.startswith("postgresql://") or url.startswith("postgres://"):
    #    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    #    url = url.replace("postgres://", "postgresql+asyncpg://", 1)

    return create_async_engine(
        url,
        # --- connection pool ---
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,  # discard stale connections
        # --- misc ---
        hide_parameters=True,  # keep bound parameters out of SQL error messages
        echo=False,  # SQL echo only in debug mode
        future=True,  # SQLAlchemy 2.0 style
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
# Lifecycle helpers – called from main.py lifespan
# ---------------------------------------------------------------------------


def _redact_db_secrets(message: str) -> str:
    """Remove the database URL and password from an error message.

    Driver errors (asyncpg, socket errors) can echo DSN fragments; the
    password must never reach logs or tracebacks.
    """
    url = make_url(settings.DATABASE_URL)
    masked = message.replace(
        settings.DATABASE_URL, url.render_as_string(hide_password=True)
    )
    if url.password:
        masked = masked.replace(str(url.password), "***")
    return masked


async def init_db() -> None:
    """Verify the database is reachable on startup."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info(
            "db.pool_init",
            url=make_url(settings.DATABASE_URL).render_as_string(hide_password=True),
        )
    except Exception as e:
        error = _redact_db_secrets(str(e))
        logger.error("db.pool_init_failed", error_type=type(e).__name__, error=error)
        # ``from None``: the chained original exception could print the raw DSN.
        raise RuntimeError(
            f"Database connection failed ({type(e).__name__}): {error}"
        ) from None


async def close_db() -> None:
    """Dispose the engine / connection pool on shutdown."""
    await engine.dispose()
    logger.info("db.pool_closed")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
