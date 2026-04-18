# conftest.py
import asyncio
import os
import sys

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from testcontainers.postgres import PostgresContainer  # type: ignore
from testcontainers.redis import RedisContainer  # type: ignore

_pg_container: PostgresContainer | None = None
_redis_container: RedisContainer | None = None

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Session-level: spin up containers and set env vars BEFORE anything imports
# ---------------------------------------------------------------------------


def pytest_configure(config):
    global _pg_container, _redis_container

    _pg_container = PostgresContainer("postgres:15-alpine")
    _pg_container.start()
    raw = _pg_container.get_connection_url()
    async_url = (
        raw.replace("postgresql+psycopg2://", "postgresql+psycopg://")
        .replace("postgresql://", "postgresql+psycopg://")
        .replace("postgres://", "postgresql+psycopg://")
    )
    os.environ["DATABASE_URL"] = async_url

    _redis_container = RedisContainer("redis:7-alpine")
    _redis_container.start()
    redis_port = _redis_container.get_exposed_port(6379)
    os.environ["REDIS_URL"] = f"redis://localhost:{redis_port}"


def pytest_unconfigure(config):
    global _pg_container, _redis_container
    if _pg_container:
        _pg_container.stop()
    if _redis_container:
        _redis_container.stop()


# ---------------------------------------------------------------------------
# Session-level: run Alembic migrations once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def init_db():
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


# ---------------------------------------------------------------------------
# Function-scoped: fresh TestClient per test
#
# Why function-scoped?
#   - Each TestClient owns a requests.Session (its own cookie jar).
#     A session-scoped client would leak refresh-token cookies between tests.
#   - The DB container and migrations stay session-scoped (expensive);
#     recreating the ASGI client is cheap.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(init_db):
    from src.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Function-scoped: truncate all auth tables after every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db(init_db):
    yield

    import src.common.database as database

    async def _truncate():
        async with database.engine.begin() as conn:
            # CASCADE handles FK ordering automatically.
            await conn.execute(
                text(
                    "TRUNCATE auth_audit_log, email_tokens, sessions, "
                    "password_credentials, users CASCADE"
                )
            )

    asyncio.run(_truncate())


# ---------------------------------------------------------------------------
# Function-scoped: capture tokens that would normally be emailed
#
# Services dispatch Celery tasks via `.delay(to, username, token)`.
# We patch those delay calls to store the latest token per address so tests
# can retrieve and use them without touching a real broker/SMTP server.
#
# Usage:
#   def test_something(client, captured_emails):
#       _register(client, username="alice")
#       token = captured_emails["alice@example.com"]
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_emails(monkeypatch) -> dict[str, str]:
    emails: dict[str, str] = {}

    def _capture(to: str, username: str, token: str) -> None:
        emails[to.lower()] = token

    monkeypatch.setattr("src.api.auth.service.send_verification_email_task.delay", _capture)
    monkeypatch.setattr("src.api.auth.service.send_password_reset_email_task.delay", _capture)
    monkeypatch.setattr("src.api.user.service.send_verification_email_task.delay", _capture)
    return emails


@pytest.fixture
def deactivate_user():
    import src.common.database as database
    from sqlalchemy import update
    from src.api.auth.models import User

    async def _run(username: str):
        async with database.engine.begin() as conn:
            await conn.execute(
                update(User).where(User.username == username).values(is_active=False)
            )

    return _run
