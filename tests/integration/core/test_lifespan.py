"""The `create_app()` lifespan: resources are bound, released and never leaked."""

import os
from typing import Never

import pytest
from sqlalchemy.engine import make_url
from taskiq import AsyncBroker

from app import main
from app.core.config import Settings
from app.core.tasks import build_broker
from app.modules.auth.tasks import send_token_email_task
from tests.helpers import (
    build_settings,
    new_test_client,
)

type _Calls = list[tuple[str, tuple[object, ...]]]


def _spy(monkeypatch: pytest.MonkeyPatch, name: str, calls: _Calls) -> None:
    """Record calls to the async cleanup `app.main.<name>` and run it."""
    real = getattr(main, name)

    async def _wrapper(*args: object, **kwargs: object) -> None:
        calls.append((name, args))
        await real(*args, **kwargs)

    monkeypatch.setattr(main, name, _wrapper)


class TestLifespan:
    def test_lifespan_binds_and_releases_the_resources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: _Calls = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)
        app = main.create_app(build_settings())

        with new_test_client(app) as c:
            factory = app.state.session_factory
            assert factory.kw["bind"] is app.state.engine
            assert c.get("/").status_code == 200

        assert [name for name, _ in calls] == [
            "close_cache",
            "close_redis",
            "close_db",
        ]
        assert app.state.session_factory.kw["bind"] is None

    def test_redis_failure_closes_the_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure after the DB is open must not leak the engine."""
        calls: _Calls = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_redis", calls)

        async def _redis_down(_settings: Settings) -> Never:
            raise ConnectionError("redis down")

        monkeypatch.setattr(main, "open_redis", _redis_down)
        app = main.create_app(build_settings())

        with pytest.raises(ConnectionError, match="redis down"), new_test_client(app):
            pass

        assert [name for name, _ in calls] == ["close_db"]
        assert calls[0][1][0] is app.state.engine
        assert app.state.session_factory.kw["bind"] is None

    def test_cache_setup_failure_closes_redis_and_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: _Calls = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)

        def _cache_setup_fails(_settings: Settings) -> Never:
            raise RuntimeError("cache setup failed")

        monkeypatch.setattr(main, "setup_cache", _cache_setup_fails)
        app = main.create_app(build_settings())

        with (
            pytest.raises(RuntimeError, match="cache setup failed"),
            new_test_client(app),
        ):
            pass

        assert [name for name, _ in calls] == ["close_redis", "close_db"]

    def test_database_failure_disposes_the_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: _Calls = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_redis", calls)
        # Wrong password on the real test database: fails fast.
        wrong_password = make_url(os.environ["DATABASE_URL"]).set(password="wrong")
        app = main.create_app(
            build_settings(
                DATABASE_URL=wrong_password.render_as_string(hide_password=False)
            )
        )

        with (
            pytest.raises(RuntimeError, match="Database connection failed"),
            new_test_client(app),
        ):
            pass

        assert [name for name, _ in calls] == ["close_db"]

    def test_lifespan_starts_and_stops_the_taskiq_broker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The broker is started, used for `.kiq()` and shut down on exit."""
        events: list[str] = []

        def _build_broker(settings: Settings) -> AsyncBroker:
            broker = build_broker(settings)
            real_startup, real_shutdown = broker.startup, broker.shutdown

            async def _startup() -> None:
                events.append("startup")
                await real_startup()

            async def _shutdown() -> None:
                events.append("shutdown")
                await real_shutdown()

            monkeypatch.setattr(broker, "startup", _startup)
            monkeypatch.setattr(broker, "shutdown", _shutdown)
            return broker

        monkeypatch.setattr(main, "build_broker", _build_broker)
        app = main.create_app(build_settings())

        with new_test_client(app):
            assert events == ["startup"]
            broker = app.state.broker
            assert send_token_email_task.kicker().broker is broker

        assert events == ["startup", "shutdown"]

    def test_broker_failure_closes_redis_and_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: _Calls = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)

        def _broken_broker(settings: Settings) -> AsyncBroker:
            broker = build_broker(settings)

            async def _startup() -> Never:
                raise ConnectionError("broker down")

            monkeypatch.setattr(broker, "startup", _startup)
            return broker

        monkeypatch.setattr(main, "build_broker", _broken_broker)
        app = main.create_app(build_settings())

        with pytest.raises(ConnectionError, match="broker down"), new_test_client(app):
            pass

        assert [name for name, _ in calls] == [
            "close_cache",
            "close_redis",
            "close_db",
        ]
