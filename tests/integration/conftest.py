"""Fixtures for the integration suite: containers, schema, per-test reset, clients.

Harness design:

* Postgres and Redis run as session-scoped fixtures, never in
  `pytest_configure`, so `pytest --collect-only` and `--help` start nothing.
* The schema comes from Alembic only (no `create_all`): the migrations are what
  production runs.
* Every test starts from an empty database and an empty Redis (`TRUNCATE` and
  `FLUSHALL` run before the test, so a crashed run cannot leak state forward).
* HTTP tests use the sync `TestClient`, also for thread races. Fixture and
  assertion SQL goes through one sync psycopg 3 engine (`db`), never through
  `asyncio.run` with the app's engine.
* psycopg async cannot run on Windows' `ProactorEventLoop`. Instead of the
  deprecated `WindowsSelectorEventLoopPolicy`, every place that creates an
  event loop passes a loop factory (`helpers.LOOP_FACTORY`; the `TestClient`
  gets it via `backend_options`). Nothing calls `set_event_loop_policy`.
* Tasks run in the request that enqueues them: the `app` fixture's broker is
  taskiq's `InMemoryBroker` with the worker's database hooks, so a mail task
  really re-keys its token in the test database. `captured_emails` records
  what the tasks hand to the mailer. Tests of the Redis transport use
  `redis_broker_client`.
* `test_environment` is a session-scoped autouse fixture, so pytest sets it
  up before every function-scoped fixture; only the session-scoped
  `sync_engine` names it.
"""

import os
from collections.abc import Callable, Iterator

import pytest
import redis as redis_sync
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from taskiq import InMemoryBroker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from app.core.config import Settings, get_settings
from app.core.logging import setup_logging
from app.core.tasks import add_task_db_hooks
from app.main import create_app
from tests.helpers import (
    BACKEND_DIR,
    POSTGRES_IMAGE,
    REDIS_IMAGE,
    CapturedEmails,
    CapturingMailer,
    Db,
    new_test_client,
)


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """The session's PostgreSQL container."""
    # driver="psycopg": the URL is postgresql+psycopg://, the app's only driver
    # (Settings refuse the testcontainers default +psycopg2).
    # Fixed names make the containers easy to find in `docker ps`; a second
    # run while one is active fails with a name conflict.
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg").with_name(
        "testing-postgres"
    ) as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    """The session's Redis container."""
    with RedisContainer(REDIS_IMAGE).with_name("testing-redis") as container:
        yield container


@pytest.fixture(scope="session", autouse=True)
def test_environment(
    postgres_container: PostgresContainer, redis_container: RedisContainer
) -> Iterator[Settings]:
    """Point every consumer (DB, cache, taskiq) at the containers.

    The app reads CACHE_HOST/CACHE_PORT and TASKIQ_*_URL (not REDIS_URL).
    Settings are cached per process, so the cache is cleared after wiring and
    again when the environment is restored. Yields the wired settings.
    """
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
            yield get_settings()
        finally:
            get_settings.cache_clear()


@pytest.fixture(scope="session")
def sync_engine(test_environment: Settings) -> Iterator[Engine]:
    """One sync psycopg 3 engine for fixtures and assertions.

    It never shares connections with the app's async engine, which lives on
    the TestClient's event loop.
    """
    engine = create_engine(test_environment.DATABASE_URL, pool_pre_ping=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_db(sync_engine: Engine) -> str:
    """Create the schema with Alembic, exactly like a deployment (once).

    Returns the comma-separated, quoted table list that `clean_db` truncates.
    """
    # No ini file: its logging section would run fileConfig(), which disables
    # every logger that exists at that moment. Which loggers exist depends on
    # what ran before (structlog creates a stdlib logger on its first event),
    # so a later assertion on such a logger would depend on the test order.
    # `upgrade` needs only the script location from the ini.
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # alembic/env.py reads DATABASE_URL from the environment.
    command.upgrade(cfg, "head")

    # Like a process start: code tested outside a lifespan logs through the
    # configured structlog/stdlib chain.
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
    return ", ".join(str(table) for table in tables)


def _flushall() -> None:
    with redis_sync.Redis(host="localhost", port=int(os.environ["CACHE_PORT"])) as r:
        r.flushall()  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of flushall() unannotated


@pytest.fixture(autouse=True)
def flush_redis() -> Callable[[], None]:
    """FLUSHALL on the Redis container before every test (not after).

    Starting from an empty Redis makes tests independent of whatever the
    previous test (or a crashed run) left behind. Returns the callable so a
    test can simulate a Redis flush mid-test.
    """
    _flushall()
    return _flushall


@pytest.fixture
def without_rate_limits() -> Iterator[None]:
    """`RATE_LIMIT_ENABLED=false` for one test, as an operator would set it.

    For tests of the mechanisms behind the limits (account lock, admission
    limiter, single-use tokens under concurrency): those must hold while the
    limits are off, which is also the state during a Redis outage
    (fail-open). The limits themselves stay on everywhere else. The checks
    read the settings per request, so the order of the fixtures does not
    matter; the environment and the settings cache are restored together.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RATE_LIMIT_ENABLED", "false")
        get_settings.cache_clear()
        try:
            yield
        finally:
            mp.undo()
            get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_db(sync_engine: Engine, migrated_db: str) -> None:
    """TRUNCATE every application table before every test (CASCADE: FK order)."""
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {migrated_db} RESTART IDENTITY CASCADE"))


def build_inline_broker(_settings: Settings) -> InMemoryBroker:
    """A broker that runs each task inside `kiq()`, on the caller's loop.

    It fires the worker events at startup, so the tasks get their
    `TaskDbSession` from the same hooks as in a worker process. A task's
    error is stored as its result, not raised into the request, as with a
    real broker.
    """
    broker = InMemoryBroker(await_inplace=True)
    add_task_db_hooks(broker)
    return broker


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A fresh app built from the wired settings; its tasks run in-process.

    The collection-time `app.main.app` was created before the containers
    existed. `create_app()` does no I/O.
    """
    monkeypatch.setattr("app.main.build_broker", build_inline_broker)
    return create_app(get_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A `TestClient` per test, with its own cookie jar.

    Refresh-token cookies cannot leak between tests. A task that failed
    fails the test: the inline broker keeps task errors in its results
    instead of raising them into the request.
    """
    with new_test_client(app) as c:
        yield c
        broker: InMemoryBroker = app.state.broker
        results = broker.result_backend.results.values()
        errors = [repr(result.error) for result in results if result.is_err]
        assert not errors, f"background tasks failed: {errors}"


@pytest.fixture
def redis_broker_client() -> Iterator[TestClient]:
    """A `TestClient` whose app enqueues on the real Redis stream broker.

    No worker runs: enqueued tasks stay in the stream for the test to read.
    """
    with new_test_client(create_app(get_settings())) as c:
        yield c


@pytest.fixture
def captured_emails(monkeypatch: pytest.MonkeyPatch) -> CapturedEmails:
    """Record every mail the tasks send, rendered, with recipient and token.

    The mail tasks run in-process (`app` fixture) and reach the SMTP
    boundary, `get_mailer()`, which returns a `CapturingMailer` here.
    Without this fixture mail is not configured in the tests, and the tasks
    send nothing.

    Usage:
        def test_something(client, captured_emails):
            register(client, username="alice")
            token = captured_emails.latest_token("alice@example.com")
            assert len(captured_emails.of_kind("verify")) == 1
            assert captured_emails == []  # nothing sent
    """
    emails = CapturedEmails()
    mailer = CapturingMailer(emails)
    # Both lookups: `send_templated_email` asks `app.core.mail`, the mail
    # task checks the configuration through its own import.
    monkeypatch.setattr("app.core.mail.get_mailer", lambda: mailer)
    monkeypatch.setattr("app.modules.auth.tasks.get_mailer", lambda: mailer)
    return emails


@pytest.fixture
def db(sync_engine: Engine) -> Db:
    """Synchronous SQL helper for assertions and direct state manipulation."""
    return Db(sync_engine)
