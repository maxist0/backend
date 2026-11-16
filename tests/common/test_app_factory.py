# tests/common/test_app_factory.py
"""create_app() and the lifespan (review 2026-11, H6/BE-04, BE-24)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

import app.main as main
from app.common.config import Settings
from tests.helpers import new_test_client

BACKEND_DIR = Path(__file__).resolve().parents[2]

STRONG = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
}


def _settings(**overrides) -> Settings:
    """Test settings: containers from the environment, no admin panel.

    The admin stays off: sqladmin keeps the session factory on the view
    classes, so a second admin-enabled app would rebind the shared views.
    """
    values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
    return Settings(_env_file=None, **values)


def _spy(monkeypatch, name: str, calls: list) -> None:
    """Record calls to the async cleanup ``app.main.<name>`` and run it."""
    real = getattr(main, name)

    async def _wrapper(*args, **kwargs):
        calls.append((name, args))
        return await real(*args, **kwargs)

    monkeypatch.setattr(main, name, _wrapper)


class TestImport:
    def test_import_without_environment_fails_clearly(self):
        """No silent fallback to dev, even next to a .env.local."""
        env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}

        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert proc.returncode != 0
        assert "ConfigurationError: ENVIRONMENT is not set" in proc.stderr


class TestCreateApp:
    def test_explicit_settings_drive_the_assembly(self):
        settings = _settings(APP_NAME="factory-app", METRICS_ENABLED=False)

        app = main.create_app(settings)

        assert app.title == "factory-app"
        assert app.state.settings is settings
        paths = {getattr(route, "path", None) for route in app.routes}
        assert "/" in paths
        assert "/metrics/" not in paths
        assert "/admin" not in paths

    def test_without_arguments_uses_get_settings(self, monkeypatch):
        settings = _settings(APP_NAME="from-get-settings")
        monkeypatch.setattr(main, "get_settings", lambda: settings)

        app = main.create_app()

        assert app.state.settings is settings
        assert app.title == "from-get-settings"

    def test_production_hides_the_api_docs(self):
        settings = Settings(_env_file=None, ENVIRONMENT="prod", **STRONG)

        app = main.create_app(settings)

        assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)

    def test_create_app_performs_no_io(self, monkeypatch):
        """Unreachable DB and Redis: assembling the app must still work."""
        settings = _settings(
            DATABASE_URL="postgresql+psycopg://u:p@127.0.0.1:1/none",
            CACHE_PORT=1,
        )

        app = main.create_app(settings)

        assert app.state.session_factory.kw.get("bind") is None


class TestLifespan:
    def test_lifespan_binds_and_releases_the_resources(self, monkeypatch):
        calls: list = []
        for name in ("close_db", "close_cache_pool", "close_route_cache"):
            _spy(monkeypatch, name, calls)
        app = main.create_app(_settings())

        with new_test_client(app) as c:
            factory = app.state.session_factory
            assert factory.kw["bind"] is app.state.engine
            assert c.get("/").status_code == 200

        assert [name for name, _ in calls] == [
            "close_route_cache",
            "close_cache_pool",
            "close_db",
        ]
        assert app.state.session_factory.kw["bind"] is None

    def test_redis_failure_closes_the_database(self, monkeypatch):
        """BE-24: a failure after the DB is open must not leak the engine."""
        calls: list = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_cache_pool", calls)

        async def _redis_down(settings):
            raise ConnectionError("redis down")

        monkeypatch.setattr(main, "init_cache_pool", _redis_down)
        app = main.create_app(_settings())

        with pytest.raises(ConnectionError, match="redis down"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_db"]
        assert calls[0][1][0] is app.state.engine
        assert app.state.session_factory.kw["bind"] is None

    def test_route_cache_failure_closes_redis_and_database(self, monkeypatch):
        calls: list = []
        for name in ("close_db", "close_cache_pool", "close_route_cache"):
            _spy(monkeypatch, name, calls)

        def _cache_setup_fails(settings):
            raise RuntimeError("route cache setup failed")

        monkeypatch.setattr(main, "init_route_cache", _cache_setup_fails)
        app = main.create_app(_settings())

        with pytest.raises(RuntimeError, match="route cache setup failed"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_cache_pool", "close_db"]

    def test_database_failure_disposes_the_engine(self, monkeypatch):
        calls: list = []
        _spy(monkeypatch, "close_db", calls)
        _spy(monkeypatch, "close_cache_pool", calls)
        # Wrong password on the real test database: fails fast.
        wrong_password = make_url(os.environ["DATABASE_URL"]).set(password="wrong")
        app = main.create_app(
            _settings(DATABASE_URL=wrong_password.render_as_string(hide_password=False))
        )

        with pytest.raises(RuntimeError, match="Database connection failed"):
            with new_test_client(app):
                pass

        assert [name for name, _ in calls] == ["close_db"]
