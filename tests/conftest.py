# conftest.py
import asyncio
import os
import sys
from dataclasses import dataclass

# MUST run before anything imports ``app``: ``app.common.taskiq`` calls
# ``init_settings(os.getenv("ENVIRONMENT", "dev"))`` at import time and would
# otherwise replace the test settings with the "dev" ones (reads .env.local).
os.environ["ENVIRONMENT"] = "unittest"
# Admin panel is opt-in outside "dev" (Phase 0 fix 6). The admin tests drive the
# real sqladmin app, so enable it explicitly for the test process.
os.environ["ADMIN_ENABLED"] = "true"

import pytest
import redis as redis_sync
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
    # The app reads CACHE_HOST/CACHE_PORT and TASKIQ_*_URL, not REDIS_URL
    # (Known issue 1). Point every Redis consumer at the container.
    os.environ["CACHE_HOST"] = "localhost"
    os.environ["CACHE_PORT"] = str(redis_port)
    os.environ["CACHE_DB"] = "0"
    os.environ["TASKIQ_BROKER_URL"] = f"redis://localhost:{redis_port}/1"
    os.environ["TASKIQ_RESULT_BACKEND_URL"] = f"redis://localhost:{redis_port}/2"


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
    from app.common.config import init_settings

    init_settings("unittest")

    import app.common.database as database

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


@pytest.fixture(autouse=True)
def flush_redis(init_db):
    """FLUSHALL on the Redis container BEFORE every test (not after).

    Starting from an empty Redis makes tests independent of whatever the
    previous test (or a crashed run) left behind.
    """

    def _flush() -> None:
        r = redis_sync.Redis(
            host="localhost", port=int(os.environ["CACHE_PORT"]), decode_responses=True
        )
        try:
            r.flushall()
        finally:
            r.close()

    _flush()
    # Yield the callable so a test can simulate a Redis outage/flush mid-test.
    yield _flush


@pytest.fixture
def client(init_db, flush_redis):
    from app.main import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Function-scoped: truncate all auth tables after every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_db(init_db):
    yield

    import app.common.database as database

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
# Function-scoped: capture every mail that would be dispatched
#
# Services dispatch Taskiq tasks via `.kiq(to, username, token)`. We patch those
# `.kiq` calls and record EVERY call as a CapturedEmail (recipient, kind, token)
# in an ordered list, so tests can also assert "no mail" and the mail count.
#
# Usage:
#   def test_something(client, captured_emails):
#       register(client, username="alice")
#       token = captured_emails["alice@example.com"]          # latest token
#       assert len(captured_emails.of_kind("verify")) == 1
#       assert captured_emails == []                           # nothing sent
#
# Backwards compatible dict-style access (latest token per address) is kept
# for the existing tests: `emails[addr]` and `addr in emails`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedEmail:
    to: str  # exactly as passed to the task (not normalised)
    kind: str  # "verify" | "reset"
    token: str
    username: str | None = None


class CapturedEmails(list):
    def to(self, address: str) -> list[CapturedEmail]:
        return [m for m in self if m.to.lower() == address.lower()]

    def of_kind(self, kind: str) -> list[CapturedEmail]:
        return [m for m in self if m.kind == kind]

    def latest_token(self, address: str, kind: str | None = None) -> str:
        mails = [m for m in self.to(address) if kind is None or m.kind == kind]
        if not mails:
            raise KeyError(address)
        return mails[-1].token

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, str):
            return self.latest_token(key)
        return super().__getitem__(key)

    def __contains__(self, item) -> bool:  # type: ignore[override]
        if isinstance(item, str):
            return bool(self.to(item))
        return super().__contains__(item)


@pytest.fixture
def captured_emails(monkeypatch) -> CapturedEmails:
    emails = CapturedEmails()

    def _make(kind: str):
        async def _capture(to: str, username: str | None = None, token: str = "", *a):
            emails.append(
                CapturedEmail(to=to, kind=kind, token=token, username=username)
            )

        return _capture

    monkeypatch.setattr(
        "app.api.auth.service.send_verification_email_task.kiq", _make("verify")
    )
    monkeypatch.setattr(
        "app.api.auth.service.send_password_reset_email_task.kiq", _make("reset")
    )
    monkeypatch.setattr(
        "app.api.user.service.send_verification_email_task.kiq", _make("verify")
    )
    return emails


@pytest.fixture
def db():
    """Synchronous SQL helper for assertions and direct state manipulation.

    Uses its own short-lived sync engine (psycopg 3 supports sync and async) so
    it never shares pooled connections with the app's event loop.
    """
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["DATABASE_URL"])

    class _Db:
        def rows(self, sql: str, **params):
            with engine.connect() as conn:
                return conn.execute(text(sql), params).mappings().all()

        def one(self, sql: str, **params):
            rows = self.rows(sql, **params)
            assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {sql}"
            return rows[0]

        def scalar(self, sql: str, **params):
            with engine.connect() as conn:
                return conn.execute(text(sql), params).scalar()

        def execute(self, sql: str, **params) -> None:
            with engine.begin() as conn:
                conn.execute(text(sql), params)

    try:
        yield _Db()
    finally:
        engine.dispose()


@pytest.fixture
def deactivate_user():
    import app.common.database as database
    from sqlalchemy import update
    from app.api.auth.models import User

    async def _run(username: str):
        async with database.engine.begin() as conn:
            await conn.execute(
                update(User).where(User.username == username).values(is_active=False)
            )

    return _run
