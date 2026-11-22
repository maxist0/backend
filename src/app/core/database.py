"""Database engine, sessions, declarative `Base`, model mixins, `value_enum`.

Nothing is created at import time. `create_app()` creates an unbound
session factory (the admin panel needs it while the app is assembled); the
lifespan builds the engine with `build_engine()`, checks it with
`init_db()` and binds the factory to it. `get_db` reads the factory from
`request.app.state`.

Usage in routers:
    from app.core import DbSession

    async def my_endpoint(db: DbSession) -> ...

Usage in models:
    from app.core import Base, Timestamps, UUIDPrimaryKey, value_enum

    class Note(UUIDPrimaryKey, Timestamps, Base): ...
"""

import asyncio
import enum
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import DateTime, Enum, MetaData, Uuid, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import Settings
from app.core.logging import get_logger

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "CreatedAt",
    "DbSession",
    "Timestamps",
    "UUIDPrimaryKey",
    "UnsupportedEventLoopError",
    "build_engine",
    "close_db",
    "create_session_factory",
    "ensure_supported_event_loop",
    "get_db",
    "init_db",
    "utcnow",
    "value_enum",
]

logger = get_logger(__name__)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC `datetime`.

    Used for timestamp columns and comparisons with them.
    """
    return datetime.now(UTC)


# SQLAlchemy's recipe
# (https://docs.sqlalchemy.org/en/21/core/constraints.html#configuring-constraint-naming-conventions):
# every constraint gets the same name in every database, so migrations can
# drop it by name and code can tell constraints apart (`constraint_name`).
# A CHECK constraint needs an explicit `name`; `value_enum` passes one.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base; every ORM model subclasses it."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    """Mixin: a UUIDv7 primary key `id`, generated in Python.

    UUIDv7 (RFC 9562) starts with a millisecond timestamp, so new rows land
    at the right end of the primary key index instead of on random pages.
    The id reveals when its row was created. A table whose ids must not
    reveal that declares its own `id` with `default=uuid.uuid4` instead of
    using this mixin.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid7, sort_order=-1
    )


class CreatedAt:
    """Mixin: `created_at`, set by the database when the row is inserted.

    The server default also covers rows written outside the ORM. The ORM
    reads the value back with `INSERT ... RETURNING`, so it is loaded after
    the flush.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), sort_order=1
    )


class Timestamps(CreatedAt):
    """Mixin: `created_at` and `updated_at`, for rows that change.

    The ORM sets `updated_at` on every UPDATE it emits, from the application
    clock: a database-side value would leave the attribute expired, and an
    async session cannot load it implicitly. An `update()` statement that
    names the column sets it itself (`updated_at=func.now()`).
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=utcnow,
        sort_order=2,
    )


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


def value_enum(
    enum_class: type[enum.Enum], *, length: int = 64, name: str | None = None
) -> Enum:
    """Column type that stores a Python enum's values in a checked VARCHAR.

    The column holds `member.value` (`"login_success"`, not
    `"LOGIN_SUCCESS"`), and a CHECK constraint named
    `ck_<table>_<name>` limits it to the current values. A PostgreSQL enum
    type is not used: it cannot drop a value. Adding or removing a value
    needs a migration that replaces the CHECK constraint; autogenerate does
    not detect it.

    Args:
        enum_class: The Python enum; its values must be strings.
        length: The VARCHAR length; room for longer future values.
        name: The CHECK constraint's name part; defaults to the lower-case
            class name. Two columns of one enum in one table need distinct
            names.

    Returns:
        The SQLAlchemy type for `mapped_column`.
    """
    return Enum(
        enum_class,
        name=name or enum_class.__name__.lower(),
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=_enum_values,
        validate_strings=True,
    )


class UnsupportedEventLoopError(RuntimeError):
    """The running event loop cannot drive the async database driver."""


def ensure_supported_event_loop() -> None:
    """Fail fast when psycopg cannot run on the current event loop.

    psycopg 3 does not support Windows' default `ProactorEventLoop`
    (https://www.psycopg.org/psycopg3/docs/advanced/async.html). uvicorn uses
    that loop on Windows for a single process; `run.py` selects a
    `SelectorEventLoop` instead, `fastapi dev` (reload) and multi-worker
    runs get one from uvicorn. The `taskiq worker` CLI always uses the
    default loop; `python -m app.worker` selects a `SelectorEventLoop`.
    Must be called inside the running loop.
    """
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        if isinstance(loop, asyncio.ProactorEventLoop):
            raise UnsupportedEventLoopError(
                "The database driver (psycopg) cannot run on Windows' "
                "ProactorEventLoop. Start the API with 'python run.py dev' or "
                "'fastapi dev' (or 'fastapi run --workers 2') and the worker "
                "with 'python -m app.worker'; they use a SelectorEventLoop."
            )


def build_engine(settings: Settings) -> AsyncEngine:
    """Create the async SQLAlchemy engine from `settings` (no I/O yet).

    Driver: psycopg 3 (`postgresql+psycopg`; SQLAlchemy picks the async
    variant for `create_async_engine`). psycopg prepares a statement on the
    server after it ran `prepare_threshold` (default 5) times on one
    connection; that is fine against PostgreSQL directly. Behind a PgBouncer
    in transaction mode it needs PgBouncer >= 1.21 with
    `max_prepared_statements`, or `prepare_threshold=None`.
    """
    url = make_url(settings.DATABASE_URL)  # validate and parse the URL
    logger.info("db.engine_creating", url=url.render_as_string(hide_password=True))

    return create_async_engine(
        url,
        # Bounded connection setup (psycopg defaults to 130 s); applies to the
        # startup check and to every new pool connection.
        connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=True,  # discard stale connections
        hide_parameters=True,  # keep bound parameters out of SQL error messages
        echo=False,
    )


def create_session_factory(
    engine: AsyncEngine | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Create the session factory; bind it later with `configure(bind=...)`."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,  # avoid implicit I/O after commit
        autoflush=False,
    )


def _redact_db_secrets(message: str, database_url: str) -> str:
    """Remove the database URL and password from an error message.

    Driver errors (psycopg, socket errors) can echo DSN fragments;
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
    except Exception as e:  # noqa: BLE001 - every failure is re-raised without the DSN
        error = _redact_db_secrets(str(e), settings.DATABASE_URL)
        logger.error("db.pool_init_failed", error_type=type(e).__name__, error=error)
        # `from None`: the chained original exception could print the raw DSN.
        raise RuntimeError(
            f"Database connection failed ({type(e).__name__}): {error}"
        ) from None


async def close_db(engine: AsyncEngine) -> None:
    """Dispose the engine / connection pool on shutdown."""
    await engine.dispose()
    logger.info("db.pool_closed")


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


# Use this alias instead of `db: AsyncSession = Depends(get_db)`.
DbSession = Annotated[AsyncSession, Depends(get_db)]
