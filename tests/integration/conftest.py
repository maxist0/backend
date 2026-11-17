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
"""

import html
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest
import redis as redis_sync
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from taskiq import InMemoryBroker
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from tests.helpers import BACKEND_DIR, new_test_client

# Same pins as compose.dev.yaml (tests/unit/core/test_dev_compose_parity.py
# fails when they drift): same major as production.
POSTGRES_IMAGE = "postgres:18.7-alpine3.24@sha256:b6972e1115a4ff8eba5d7f7d19184926d1e16d7d1136ae1e1a7ebf286da0f015"
REDIS_IMAGE = "redis:7.4.12-alpine3.22@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe"


@pytest.fixture(scope="session")
def postgres_container():
    # driver="psycopg": the URL is postgresql+psycopg://, the app's only driver
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
    from app.core.config import get_settings

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

    Returns the comma-separated, quoted table list that `clean_db` truncates.
    """
    from app.core.config import get_settings
    from app.core.logging import setup_logging

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


@pytest.fixture(autouse=True)
def flush_redis(test_environment):
    """FLUSHALL on the Redis container before every test (not after).

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
    """TRUNCATE every application table before every test (CASCADE: FK order)."""
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {migrated_db} RESTART IDENTITY CASCADE"))
    yield


def build_inline_broker(_settings) -> InMemoryBroker:
    """A broker that runs each task inside `kiq()`, on the caller's loop.

    It fires the worker events at startup, so the tasks get their
    `TaskDbSession` from the same hooks as in a worker process. A task's
    error is stored as its result, not raised into the request, as with a
    real broker.
    """
    from app.core.tasks import add_task_db_hooks

    broker = InMemoryBroker(await_inplace=True)
    add_task_db_hooks(broker)
    return broker


@pytest.fixture
def app(test_environment, monkeypatch):
    """A fresh app built from the wired settings; its tasks run in-process.

    The collection-time `app.main.app` was created before the containers
    existed. `create_app()` does no I/O.
    """
    from app import main
    from app.core.config import get_settings

    monkeypatch.setattr(main, "build_broker", build_inline_broker)
    return main.create_app(get_settings())


@pytest.fixture
def client(app, clean_db, flush_redis):
    """A `TestClient` per test: each owns its cookie jar, so refresh-token
    cookies cannot leak between tests.

    A task that failed fails the test: the inline broker keeps task errors
    in its results instead of raising them into the request.
    """
    with new_test_client(app) as c:
        yield c
        results = c.app.state.broker.result_backend.results.values()
        errors = [repr(result.error) for result in results if result.is_err]
        assert not errors, f"background tasks failed: {errors}"


@pytest.fixture
def redis_broker_client(test_environment, clean_db, flush_redis):
    """A `TestClient` whose app enqueues on the real Redis stream broker.

    No worker runs: enqueued tasks stay in the stream for the test to read.
    """
    from app.core.config import get_settings
    from app.main import create_app

    with new_test_client(create_app(get_settings())) as c:
        yield c


def run_task(client, task, *args):
    """Enqueue `task` on the client app's broker and return once it ran.

    With the `app` fixture's inline broker the task runs before this
    returns, on the app's event loop, with the app's task database.
    """

    async def _kiq():
        return await task.kiq(*args)

    return client.portal.call(_kiq)


# The subject tells the mails apart (`app.modules.auth.tasks`).
_KIND_BY_SUBJECT = {
    "Verify your email address": "verify",
    "Reset your password": "reset",
    "Confirm your new email address": "change_email",
}
_LINK = re.compile(r'href="([^"]+)"')


@dataclass(frozen=True)
class CapturedEmail:
    to: str
    kind: str  # "verify" | "reset" | "change_email"
    token: str  # read from the link in the body
    username: str
    link: str
    html: str

    @classmethod
    def from_message(cls, message) -> "CapturedEmail":
        [recipient] = message.recipients
        link = html.unescape(_LINK.search(message.body).group(1))
        [token] = parse_qs(urlsplit(link).query)["token"]
        return cls(
            to=recipient.email,
            kind=_KIND_BY_SUBJECT[message.subject],
            token=token,
            username=recipient.name,
            link=link,
            html=message.body,
        )


class CapturedEmails(list):
    """Every dispatched mail, in order, so tests can assert "no mail" too.

    Dict-style access (`emails[addr]`, `addr in emails`) returns the latest
    token per address.
    """

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


class CapturingMailer:
    """Stands in for fastapi-mail's `FastMail`; records instead of sending."""

    def __init__(self, emails: CapturedEmails) -> None:
        self.emails = emails

    async def send_message(self, message) -> None:
        self.emails.append(CapturedEmail.from_message(message))


@pytest.fixture
def captured_emails(monkeypatch) -> CapturedEmails:
    """Record every mail the tasks send, rendered, with recipient and token.

    The mail tasks run in-process (`app` fixture) and reach the SMTP
    boundary, `get_mailer()`, which returns a `CapturingMailer` here.
    Without this fixture mail is not configured in the tests, and the tasks
    send nothing.

    Usage:
        def test_something(client, captured_emails):
            register(client, username="alice")
            token = captured_emails["alice@example.com"]  # latest token
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
