"""Library behaviour the app relies on that needs Postgres or Redis.

Covers the SQLAlchemy 2.1, redis-py 8, cashews 7.6 and taskiq-redis upgrades.
Each test names the change it covers.
"""

import asyncio
import os
import uuid
import warnings

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from taskiq.message import BrokerMessage
from taskiq_redis import ListQueueBroker

from app.modules.auth.models import AuditEventType, AuthAuditLog
from app.core.cache import close_cache_pool, init_cache_pool
from app.core.config import get_settings
from app.core.database import build_engine, create_session_factory
from tests.helpers import run_async


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


class TestTaskiqBroker:
    def test_idle_worker_queue_does_not_time_out(self):
        """redis-py 8 REGRESSION GUARD (takes ~6 s).

        redis-py 8 defaults to a 5 s socket read timeout; taskiq-redis waits
        for jobs with BRPOP without a server timeout and retries only
        ConnectionError, so an idle worker failed with
        ``redis.exceptions.TimeoutError`` after 5 s. The app's broker sets
        ``socket_timeout=None``; with that setting the listener keeps waiting
        and still receives the next job.
        """
        from app.core.tasks import build_broker

        app_broker = build_broker(get_settings())

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
