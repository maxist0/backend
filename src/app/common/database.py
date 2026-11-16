# src/app/common/database.py

"""
Database engine, session factory, and declarative Base for SQLAlchemy 2.0.

Nothing is created at import time. ``create_app()`` creates an unbound
session factory (the admin panel needs it while the app is assembled); the
lifespan builds the engine with ``build_engine()``, checks it with
``init_db()`` and binds the factory to it. ``get_db`` reads the factory from
``request.app.state``.

Usage in routers / services:
    from app.common.dependencies import DbSession

    async def my_endpoint(db: DbSession) -> ...
"""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.common.config import Settings
from app.common.logging import get_logger
from app.common.models import Base

__all__ = [
    "Base",
    "build_engine",
    "close_db",
    "create_session_factory",
    "get_db",
    "init_db",
]

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Engine and session factory (created by the app factory / lifespan)
# ---------------------------------------------------------------------------


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async SQLAlchemy engine from *settings* (no I/O yet)."""
    url = make_url(settings.DATABASE_URL)  # validate and parse the URL
    logger.info("db.engine_creating", url=url.render_as_string(hide_password=True))

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
        echo=False,
    )


def create_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Create the session factory; bind it later with ``configure(bind=...)``."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,  # avoid implicit I/O after commit
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers – called from the lifespan in main.py
# ---------------------------------------------------------------------------


def _redact_db_secrets(message: str, database_url: str) -> str:
    """Remove the database URL and password from an error message.

    Driver errors (asyncpg, psycopg, socket errors) can echo DSN fragments;
    the password must never reach logs or tracebacks.
    """
    url = make_url(database_url)
    masked = message.replace(database_url, url.render_as_string(hide_password=True))
    if url.password:
        masked = masked.replace(str(url.password), "***")
    return masked


async def init_db(engine: AsyncEngine, settings: Settings) -> None:
    """Verify the database is reachable on startup."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info(
            "db.pool_init",
            url=make_url(settings.DATABASE_URL).render_as_string(hide_password=True),
        )
    except Exception as e:
        error = _redact_db_secrets(str(e), settings.DATABASE_URL)
        logger.error("db.pool_init_failed", error_type=type(e).__name__, error=error)
        # ``from None``: the chained original exception could print the raw DSN.
        raise RuntimeError(
            f"Database connection failed ({type(e).__name__}): {error}"
        ) from None


async def close_db(engine: AsyncEngine) -> None:
    """Dispose the engine / connection pool on shutdown."""
    await engine.dispose()
    logger.info("db.pool_closed")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session from the factory the lifespan bound to the engine."""
    session_factory: async_sessionmaker[AsyncSession] = (
        request.app.state.session_factory
    )
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
