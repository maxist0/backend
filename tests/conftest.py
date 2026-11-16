# conftest.py
"""Shared fixtures: containers, schema, per-test reset, clients.

Harness design (review 2026-11, step 1.3):

* Postgres and Redis run as SESSION-scoped fixtures, never in
  ``pytest_configure``: ``pytest --collect-only`` and ``--help`` start nothing.
* The schema comes from Alembic only (no ``create_all``): the migrations are
  what production runs.
* Every test starts from an empty database and an empty Redis (``TRUNCATE`` and
  ``FLUSHALL`` BEFORE the test, so a crashed run cannot leak state forward).
* HTTP tests use the sync ``TestClient`` (also for thread races). Fixture and
  assertion SQL goes through one sync psycopg 3 engine (``db``), never through
  ``asyncio.run`` with the app's engine.
* psycopg async cannot run on Windows' ProactorEventLoop. Instead of the
  deprecated ``WindowsSelectorEventLoopPolicy``, every place that creates an
  event loop passes a loop factory (``helpers.LOOP_FACTORY``; the TestClient
  gets it via ``backend_options``). No ``set_event_loop_policy`` anywhere.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# MUST run before anything calls ``get_settings()``: ENVIRONMENT is required
# (no fallback to dev), and "unittest" never reads .env.local.
os.environ["ENVIRONMENT"] = "unittest"
# Admin panel is opt-in outside "dev" (Phase 0 fix 6). The admin tests drive the
# real sqladmin app, so enable it explicitly for the test process.
os.environ["ADMIN_ENABLED"] = "true"
# Collection imports app modules (``app.main`` builds an app, ``app.common.taskiq``
# builds the broker) BEFORE the containers exist and their ports are known.
# Until ``test_environment`` wires the real addresses, every service address
# points at a closed port, so nothing collected or imported early can reach a
# developer's own Postgres/Redis on the default ports. Tests never use those
# early objects: ``client`` builds its app from the wired settings. (The broker
# is the exception; its enqueue stays unreachable on purpose, tests that
# register users use ``captured_emails``. Step 1.5 moves the broker into the
# lifespan.)
_UNREACHABLE = "127.0.0.1:1"
os.environ["DATABASE_URL"] = f"postgresql+psycopg://x:x@{_UNREACHABLE}/unconfigured"
os.environ["CACHE_HOST"] = "127.0.0.1"
os.environ["CACHE_PORT"] = "1"
os.environ["TASKIQ_BROKER_URL"] = f"redis://{_UNREACHABLE}/1"
os.environ["TASKIQ_RESULT_BACKEND_URL"] = f"redis://{_UNREACHABLE}/2"

import pytest  # noqa: E402
import redis as redis_sync  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from testcontainers.postgres import PostgresContainer  # type: ignore  # noqa: E402
from testcontainers.redis import RedisContainer  # type: ignore  # noqa: E402

from tests.helpers import new_test_client  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Same pins as compose.dev.yaml (tests/common/test_dev_compose_parity.py fails
# when they drift): same major as production (review 2026-11, N3).
POSTGRES_IMAGE = "postgres:18.7-alpine3.24@sha256:b6972e1115a4ff8eba5d7f7d19184926d1e16d7d1136ae1e1a7ebf286da0f015"
REDIS_IMAGE = "redis:7.4.12-alpine3.22@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe"


# ---------------------------------------------------------------------------
# Session-level: containers, environment wiring, schema
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_container():
    # driver="psycopg": URL is postgresql+psycopg://, the app's only driver
    # (Settings refuse the testcontainers default +psycopg2).
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        yield container


@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer(REDIS_IMAGE) as container:
        yield container


@pytest.fixture(scope="session", autouse=True)
def test_environment(postgres_container, redis_container):
    """Point every consumer (DB, cache, taskiq) at the containers.

    The app reads CACHE_HOST/CACHE_PORT and TASKIQ_*_URL (not REDIS_URL).
    Settings are cached per process, so the cache is cleared after wiring and
    again when the environment is restored.
    """
    from app.common.config import get_settings

    redis_port = redis_container.get_exposed_port(6379)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", postgres_container.get_connection_url())
        mp.setenv("CACHE_HOST", "localhost")
        mp.setenv("CACHE_PORT", str(redis_port))
        mp.setenv("CACHE_DB", "0")
        mp.setenv("TASKIQ_BROKER_URL", f"redis://localhost:{redis_port}/1")
        mp.setenv("TASKIQ_RESULT_BACKEND_URL", f"redis://localhost:{redis_port}/2")
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture(scope="session")
def sync_engine(test_environment):
    """One sync psycopg 3 engine for fixtures and assertions.

    It never shares connections with the app's async engine, which lives on
    the TestClient's event loop.
    """
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_db(sync_engine):
    """Create the schema with Alembic, exactly like a deployment (once).

    Returns the comma-separated, quoted table list that ``clean_db`` truncates.
    """
    from app.common.config import get_settings
    from app.common.logging import setup_logging

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # alembic/env.py reads DATABASE_URL from the environment.
    command.upgrade(cfg, "head")

    # Like a process start: code tested outside a lifespan logs through the
    # configured structlog/stdlib chain. After alembic, whose fileConfig
    # replaces the root handlers.
    setup_logging(get_settings())

    # Every table except alembic's own bookkeeping, so new models are reset
    # without touching this file.
    with sync_engine.connect() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            )
            .scalars()
            .all()
        )
    assert tables, "alembic upgrade head created no tables"
    return ", ".join(tables)


# ---------------------------------------------------------------------------
# Function-scoped: reset state BEFORE every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def flush_redis(test_environment):
    """FLUSHALL on the Redis container BEFORE every test (not after).

    Starting from an empty Redis makes tests independent of whatever the
    previous test (or a crashed run) left behind. Yields the callable so a test
    can simulate a Redis outage/flush mid-test.
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
    yield _flush


@pytest.fixture(autouse=True)
def clean_db(sync_engine, migrated_db):
    """TRUNCATE every application table BEFORE every test (CASCADE: FK order)."""
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {migrated_db} RESTART IDENTITY CASCADE"))
    yield


# ---------------------------------------------------------------------------
# Function-scoped: fresh app and TestClient per test
#
# Why function-scoped?
#   - Each TestClient owns its own cookie jar. A session-scoped client would
#     leak refresh-token cookies between tests.
#   - The app is built from the WIRED settings (the collection-time
#     ``app.main.app`` was created before the containers existed).
#     create_app() does no I/O.
#   - Containers and migrations stay session-scoped (expensive).
# ---------------------------------------------------------------------------


@pytest.fixture
def app(test_environment):
    from app.common.config import get_settings
    from app.main import create_app

    return create_app(get_settings())


@pytest.fixture
def client(app, clean_db, flush_redis):
    with new_test_client(app) as c:
        yield c


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
def db(sync_engine):
    """Synchronous SQL helper for assertions and direct state manipulation.

    Shares the session's sync psycopg 3 engine; it never touches the app's
    event loop or its async engine.
    """
    engine = sync_engine

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

    return _Db()
