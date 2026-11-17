"""The `create_app()` lifespan: resources are bound, released and never leaked."""

import os

import pytest
from sqlalchemy.engine import make_url

import app.main as main
from app.core.config import Settings
from tests.helpers import new_test_client


def _settings(**overrides) -> Settings:
    """Test settings: containers from the environment, no admin panel.

    The admin stays off: sqladmin keeps the session factory on the view
    classes, so a second admin-enabled app would rebind the shared views.
    """
    values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
    return Settings(_env_file=None, **values)


def _spy(monkeypatch, name: str, calls: list) -> None:
    """Record calls to the async cleanup `app.main.<name>` and run it."""
    real = getattr(main, name)

    async def _wrapper(*args, **kwargs):
        calls.append((name, args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(main, name, _wrapper)


class TestLifespan:
    def test_lifespan_binds_and_releases_the_resources(self, monkeypatch):
        calls: list = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)
        app = main.create_app(_settings())

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

    def test_redis_failure_closes_the_database(self, monkeypatch):
        """A failure after the DB is open must not leak the engine."""
        calls: list = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_redis", calls)

        async def _redis_down(settings):
            raise ConnectionError("redis down")

        monkeypatch.setattr(main, "open_redis", _redis_down)
        app = main.create_app(_settings())

        with pytest.raises(ConnectionError, match="redis down"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_db"]
        assert calls[0][1][0] is app.state.engine
        assert app.state.session_factory.kw["bind"] is None

    def test_cache_setup_failure_closes_redis_and_database(self, monkeypatch):
        calls: list = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)

        def _cache_setup_fails(settings):
            raise RuntimeError("cache setup failed")

        monkeypatch.setattr(main, "setup_cache", _cache_setup_fails)
        app = main.create_app(_settings())

        with pytest.raises(RuntimeError, match="cache setup failed"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_redis", "close_db"]

    def test_database_failure_disposes_the_engine(self, monkeypatch):
        calls: list = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_redis", calls)
        # Wrong password on the real test database: fails fast.
        wrong_password = make_url(os.environ["DATABASE_URL"]).set(password="wrong")
        app = main.create_app(
            _settings(DATABASE_URL=wrong_password.render_as_string(hide_password=False))
        )

        with pytest.raises(RuntimeError, match="Database connection failed"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_db"]

    def test_lifespan_starts_and_stops_the_taskiq_broker(self, monkeypatch):
        """The broker is started, used for `.kiq()` and shut down on exit."""
        from app.modules.auth.tasks import send_verification_email_task

        events: list[str] = []
        real_build_broker = main.build_broker

        def _build_broker(settings):
            broker = real_build_broker(settings)
            real_startup, real_shutdown = broker.startup, broker.shutdown

            async def _startup():
                events.append("startup")
                await real_startup()

            async def _shutdown():
                events.append("shutdown")
                await real_shutdown()

            broker.startup, broker.shutdown = _startup, _shutdown
            return broker

        monkeypatch.setattr(main, "build_broker", _build_broker)
        app = main.create_app(_settings())

        with new_test_client(app):
            assert events == ["startup"]
            broker = app.state.broker
            assert send_verification_email_task.kicker().broker is broker

        assert events == ["startup", "shutdown"]

    def test_broker_failure_closes_redis_and_database(self, monkeypatch):
        calls: list = []
        for name in ("close_db", "close_redis", "close_cache"):
            _spy(monkeypatch, name, calls)
        real_build_broker = main.build_broker

        def _broken_broker(settings):
            broker = real_build_broker(settings)

            async def _startup():
                raise ConnectionError("broker down")

            broker.startup = _startup
            return broker

        monkeypatch.setattr(main, "build_broker", _broken_broker)
        app = main.create_app(_settings())

        with pytest.raises(ConnectionError, match="broker down"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == [
            "close_cache",
            "close_redis",
            "close_db",
        ]
