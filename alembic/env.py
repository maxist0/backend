"""Alembic environment for async migrations.

The database URL comes from `DATABASE_URL` only. With `ENVIRONMENT=dev` a
missing variable is filled from `.env.local` (`override=False`: injected
variables always win). No fallback to dev: without `ENVIRONMENT=dev` the
file is never read. The application settings are not built here, so a
migration job needs no application secrets.

Run autogenerate:
    ENVIRONMENT=dev alembic revision --autogenerate -m "description"

Apply migrations:
    ENVIRONMENT=dev alembic upgrade head
    DATABASE_URL=postgresql+psycopg://... alembic upgrade head

The URL is normalised like the application's (`postgresql://` and
`postgres://` become `postgresql+psycopg://`; other drivers are refused).

Downgrade one step:
    ENVIRONMENT=dev alembic downgrade -1
"""

import asyncio
import logging
import os
import sys
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

# Importing app.modules imports every registered module (MODULES), and with
# it each module's models, so the metadata holds all tables (autogenerate
# must never see an empty metadata). It reads no settings.
import app.modules  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every module's tables on Base.metadata
from app.core import Base
from app.core.config import LOCAL_ENV_FILE, Environment, normalize_database_url


class MigrationConfigError(RuntimeError):
    """The environment does not name a database to migrate."""


config = context.config

# fileConfig() disables every logger that exists when it runs. An in-process
# caller that must keep its loggers passes `Config()` without a file;
# `setup_logging` re-enables uvicorn's loggers.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")


def _database_url() -> tuple[str, str]:
    """Resolve the migration URL and name where it came from.

    Returns:
        The normalised URL and its source: `environment` or the file name
        of `.env.local`.
    """
    injected = bool(os.environ.get("DATABASE_URL", "").strip())
    environment = os.environ.get("ENVIRONMENT", "").strip()
    if environment == Environment.DEV.value and LOCAL_ENV_FILE.exists():
        load_dotenv(LOCAL_ENV_FILE, override=False)

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise MigrationConfigError(
            "DATABASE_URL is not set. Export it, or set ENVIRONMENT=dev to "
            "read it from .env.local."
        )
    try:
        return normalize_database_url(url), (
            "environment" if injected else LOCAL_ENV_FILE.name
        )
    except ValueError as exc:
        # The message names the scheme only, never the URL.
        raise MigrationConfigError(str(exc)) from None


_url, _url_source = _database_url()
# The source is logged so a shell with a global ENVIRONMENT=dev cannot
# silently migrate the .env.local database instead of the intended one.
logger.info(
    "Using DATABASE_URL %s (from %s)",
    make_url(_url).render_as_string(hide_password=True),
    _url_source,
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live connection."""
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on a connection (sync side of `run_sync`)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync wrapper."""
    connectable = create_async_engine(
        _url,
        poolclass=pool.NullPool,  # no pool needed for one-shot migration runs
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run the migrations against the database."""
    # psycopg (async) cannot use Windows' default ProactorEventLoop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(run_async_migrations(), loop_factory=loop_factory)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

logger.info("Migration complete")
