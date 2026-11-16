# tests/common/test_upgrade_libraries.py
"""Library behaviour the app relies on after the step 1.4 upgrades (2026-11-16).

SQLAlchemy 2.0.49 -> 2.1.8, redis-py 7.4 -> 8.1, cashews 7.5 -> 7.6,
taskiq 0.12.2 -> 0.13.0 / taskiq-redis 1.2.2 -> 1.2.4, OpenTelemetry
1.39 -> 1.45. Each test names the change it covers.
"""

import asyncio
import importlib
import importlib.metadata
import os
import subprocess
import sys
import tomllib
import uuid
import warnings
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from taskiq.message import BrokerMessage
from taskiq_redis import ListQueueBroker

import app.common.logging as app_logging
from app.api.auth.models import AuditEventType, AuthAuditLog
from app.common.cache import close_cache_pool, init_cache_pool
from app.common.config import Settings, get_settings
from app.common.database import build_engine, create_session_factory
from tests.helpers import run_async

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _settings(**overrides) -> Settings:
    values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------------------
# SQLAlchemy 2.1
# ---------------------------------------------------------------------------


class TestSqlAlchemy21:
    def test_app_sessions_do_not_autoflush_before_core_statements(self):
        """2.1: autoflush is unconditional, i.e. also before Core/text() SQL.

        The app's session factory sets ``autoflush=False``, so pending objects
        stay invisible to a raw statement in the same session (unchanged).
        The control session with ``autoflush=True`` shows the 2.1 change: on
        2.0 a ``text()`` statement did not autoflush and counted 0.
        """
        count = text("SELECT count(*) FROM auth_audit_log")

        async def scenario() -> tuple[int, int]:
            engine = build_engine(get_settings())
            try:
                async with create_session_factory(engine)() as session:
                    session.add(AuthAuditLog(event_type=AuditEventType.REGISTER))
                    app_session = await session.scalar(count)
                    await session.rollback()
                autoflushing = async_sessionmaker(engine, autoflush=True)
                async with autoflushing() as session:
                    session.add(AuthAuditLog(event_type=AuditEventType.REGISTER))
                    control = await session.scalar(count)
                    await session.rollback()
            finally:
                await engine.dispose()
            return app_session, control

        assert run_async(scenario()) == (0, 1)

    def test_select_is_typed_per_column(self, tmp_path):
        """2.1: ``select(a, b)`` is ``Select[A, B]`` (2.0: ``Select[tuple[A, B]]``).

        Checked with the project's mypy and configuration on a snippet.
        """
        snippet = tmp_path / "select_typing.py"
        snippet.write_text(
            "import uuid\n"
            "from typing import assert_type\n"
            "from sqlalchemy import Select, select\n"
            "from app.api.auth.models import User\n"
            "assert_type(select(User.id, User.username), Select[uuid.UUID, str])\n",
            encoding="utf-8",
        )

        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", "pyproject.toml"]
            + [str(snippet)],
            cwd=BACKEND_DIR,
            # Resolve ``app`` from the source tree (the installed package has
            # no py.typed marker yet; mypy strict is step 1.6).
            env={**os.environ, "MYPYPATH": str(BACKEND_DIR / "src")},
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]

    def test_greenlet_comes_with_the_asyncio_extra(self):
        """2.1: greenlet is no longer a default dependency, only ``[asyncio]``."""
        pyproject = tomllib.loads(
            (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )
        requires = importlib.metadata.requires("sqlalchemy") or []

        assert any(
            dep.startswith("sqlalchemy[asyncio]")
            for dep in pyproject["project"]["dependencies"]
        )
        assert any(
            req.startswith("greenlet") and 'extra == "asyncio"' in req
            for req in requires
        )
        importlib.import_module("greenlet")


# ---------------------------------------------------------------------------
# redis-py 8 and cashews
# ---------------------------------------------------------------------------


class TestRedisPy8:
    def test_cache_client_roundtrip_and_close(self):
        """8.0: RESP3 on the wire, legacy response shapes, bounded socket timeouts.

        Values stay ``bytes`` (``decode_responses`` default unchanged), the
        async ``ping()`` is typed ``Awaitable[bool]`` (no ``type: ignore``),
        ``aclose()`` raises no deprecation, and reads now time out after
        redis-py's new default of 5 s instead of never.
        """

        async def scenario() -> tuple[bytes | None, object]:
            client = await init_cache_pool(get_settings())
            try:
                await client.set("upgrade:redis8", "v", ex=60)
                value = await client.get("upgrade:redis8")
                timeout = client.connection_pool.connection_kwargs["socket_timeout"]
            finally:
                await close_cache_pool(client)
            return value, timeout

        assert run_async(scenario()) == (b"v", 5)

    def test_cashews_still_calls_the_deprecated_redis_close(self):
        """Exit condition for the cashews ignore in ``[tool.pytest.ini_options]``.

        cashews 7.6.0 closes its redis client with ``close()`` (deprecated in
        redis-py 5.0.1). When this test fails, cashews uses ``aclose()``:
        delete the ignore and this test.
        """
        from cashews import Cache

        cache = Cache()
        cache.setup(f"redis://{os.environ['CACHE_HOST']}:{os.environ['CACHE_PORT']}/0")

        async def scenario() -> list[str]:
            await cache.set("upgrade:cashews", "v", expire=60)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await cache.close()
            return [str(w.message) for w in caught if w.category is DeprecationWarning]

        messages = run_async(scenario())

        assert any("Call to deprecated close" in m for m in messages)


# ---------------------------------------------------------------------------
# taskiq 0.13 / taskiq-redis 1.2.4 on redis-py 8
# ---------------------------------------------------------------------------


class TestTaskiqBroker:
    def test_tasks_are_registered_on_the_broker(self):
        """0.13: broker API unchanged; both mail tasks are registered."""
        from app.common.taskiq import broker
        from app.utils.email_tasks import (
            send_password_reset_email_task,
            send_verification_email_task,
        )

        names = set(broker.get_all_tasks())

        assert {
            send_verification_email_task.task_name,
            send_password_reset_email_task.task_name,
        } <= names

    def test_idle_worker_queue_does_not_time_out(self):
        """redis-py 8 REGRESSION GUARD (takes ~6 s).

        redis-py 8 defaults to a 5 s socket read timeout; taskiq-redis waits
        for jobs with BRPOP without a server timeout and retries only
        ConnectionError, so an idle worker failed with
        ``redis.exceptions.TimeoutError`` after 5 s. The app's broker sets
        ``socket_timeout=None``; with that setting the listener keeps waiting
        and still receives the next job.
        """
        from app.common.taskiq import broker as app_broker

        socket_timeout = app_broker.connection_pool.connection_kwargs["socket_timeout"]
        assert socket_timeout is None

        async def scenario() -> bytes:
            broker = ListQueueBroker(
                os.environ["TASKIQ_BROKER_URL"],
                queue_name=f"upgrade-{uuid.uuid4().hex}",
                socket_timeout=socket_timeout,
            )
            await broker.startup()
            try:

                async def first_message() -> bytes:
                    async for message in broker.listen():
                        return message
                    raise AssertionError("listen() ended")

                try:
                    await asyncio.wait_for(first_message(), timeout=6)
                except TimeoutError:
                    pass  # asyncio's timeout: still waiting, as intended
                else:
                    raise AssertionError("received a message from an empty queue")

                await broker.kick(
                    BrokerMessage(
                        task_id="upgrade-check",
                        task_name="upgrade-check",
                        message=b"payload",
                        labels={},
                    )
                )
                return await asyncio.wait_for(first_message(), timeout=5)
            finally:
                await broker.shutdown()

        assert run_async(scenario()) == b"payload"


# ---------------------------------------------------------------------------
# OpenTelemetry 1.45
# ---------------------------------------------------------------------------


class TestOtlpLogHandler:
    def test_handler_builds_without_the_sdk_deprecation(self, monkeypatch):
        """1.40: the SDK ``LoggingHandler`` warns (DeprecationWarning) in __init__.

        pytest turns deprecations into errors; ``_build_otlp_handler``
        silences only that one message (see app.common.logging). The global
        provider is not replaced, so no other test is affected.
        """
        import opentelemetry._logs as otel_logs
        from opentelemetry.sdk._logs import LoggingHandler

        providers: list = []
        monkeypatch.setattr(otel_logs, "set_logger_provider", providers.append)
        settings = _settings(OTLP_LOGS_ENABLED=True, OTLP_ENDPOINT="http://127.0.0.1:1")

        handler = app_logging._build_otlp_handler(settings)
        try:
            assert isinstance(handler, LoggingHandler)
            assert len(providers) == 1
        finally:
            for provider in providers:
                provider.shutdown()
