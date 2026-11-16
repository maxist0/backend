# alembic/env.py
"""
Alembic environment for async (asyncpg) migrations.

Run autogenerate:
    alembic revision --autogenerate -m "description"

Apply migrations:
    alembic upgrade head
    alembic -x env=test upgrade head

Downgrade one step:
    alembic downgrade -1
"""

import asyncio
from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

# ── Import the project's declarative Base so Alembic can detect schema changes.
# Add additional model modules here as the project grows.
from src.common.models import Base  # noqa: F401 – registers metadata
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Alembic Config object – gives access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config


_env_file = ".env.local"

# Only local development reads .env.local (mirrors src.common.config.get_env_file).
# override=False: variables injected by the runtime or the test harness always win.
if os.getenv("ENVIRONMENT", "dev") == "dev" and os.path.exists(_env_file):
    load_dotenv(_env_file, override=False)

# Inject the live DATABASE_URL so we never hard-code it in alembic.ini
_url = os.getenv("DATABASE_URL")
assert _url, "DATABASE_URL environment variable must be set for Alembic migrations"

print(
    "[alembic] Using DATABASE_URL: "
    f"{make_url(_url).render_as_string(hide_password=True)}"
)

if _url.startswith("postgresql://") or _url.startswith("postgres://"):
    _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)
    _url = _url.replace("postgres://", "postgresql+asyncpg://", 1)
config.set_main_option("sqlalchemy.url", _url)

# Set up Python logging from alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata that Alembic inspects for autogenerate
target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Offline migrations (no live DB connection)
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Generate SQL scripts without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (async engine)
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
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
        config.get_main_option("sqlalchemy.url"),  # type: ignore[arg-type]
        poolclass=pool.NullPool,  # no pool needed for one-shot migration runs
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

print("[alembic] Migration complete")
