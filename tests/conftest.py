# conftest.py
import os
import sys
import asyncio
from alembic import command
from alembic.config import Config
import pytest
from fastapi.testclient import TestClient
from testcontainers.postgres import PostgresContainer

_container: PostgresContainer | None = None

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

def pytest_configure(config):
    global _container
    _container = PostgresContainer("postgres:15-alpine")
    _container.start()

    raw = _container.get_connection_url()

    # testcontainers returns postgresql+psycopg2:// — swap to the async driver
    async_url = (
        raw
        .replace("postgresql+psycopg2://", "postgresql+psycopg://")
        .replace("postgresql://", "postgresql+psycopg://")
        .replace("postgres://", "postgresql+psycopg://")
    )

    os.environ["DATABASE_URL"] = async_url


def pytest_unconfigure(config):
    """Tear down container after the session."""
    global _container
    if _container:
        _container.stop()


@pytest.fixture(scope="session", autouse=True)
def init_db():
    """Initialize settings + schema once DATABASE_URL is guaranteed to be set."""
    from src.common.config import init_settings
    init_settings("unittest")

    import src.common.database as database

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

    command.upgrade(alembic_cfg, "head")

    async def _create():
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    asyncio.run(_create())

    yield

    async def _drop():
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)

    asyncio.run(_drop())


@pytest.fixture(scope="session")
def client(init_db):
    from src.main import app
    with TestClient(app) as c:
        yield c