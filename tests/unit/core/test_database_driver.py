"""psycopg 3 as the only driver: URL normalisation, engine options and the
Windows event-loop guard.
"""

import asyncio
import sys

import pytest
from pydantic import ValidationError

import app.common.database as database
from app.common.config import normalize_database_url
from tests.helpers import LOOP_FACTORY

SECRET = "pw-must-not-leak"


class TestDatabaseUrlNormalisation:
    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql://u:{SECRET}@db:5432/api",
            f"postgres://u:{SECRET}@db:5432/api",
            f"POSTGRESQL://u:{SECRET}@db:5432/api",
            f"postgresql+psycopg://u:{SECRET}@db:5432/api",
            f"  postgresql+psycopg://u:{SECRET}@db:5432/api  ",
        ],
    )
    def test_generic_and_psycopg_urls_become_psycopg(self, url):
        assert (
            normalize_database_url(url)
            == f"postgresql+psycopg://u:{SECRET}@db:5432/api"
        )

    def test_query_parameters_are_kept(self):
        url = f"postgres://u:{SECRET}@db/api?sslmode=require&connect_timeout=5"

        assert normalize_database_url(url) == (
            f"postgresql+psycopg://u:{SECRET}@db/api?sslmode=require&connect_timeout=5"
        )

    def test_asyncpg_is_refused_with_a_hint(self):
        with pytest.raises(ValueError, match="asyncpg") as excinfo:
            normalize_database_url(f"postgresql+asyncpg://u:{SECRET}@db/api")

        assert "postgresql+psycopg://" in str(excinfo.value)
        assert SECRET not in str(excinfo.value)

    @pytest.mark.parametrize(
        "url",
        [
            f"postgresql+psycopg2://u:{SECRET}@db/api",
            f"mysql://u:{SECRET}@db/api",
            f"u:{SECRET}@db/api",
        ],
    )
    def test_other_drivers_and_schemeless_urls_are_refused(self, url):
        with pytest.raises(ValueError) as excinfo:
            normalize_database_url(url)

        assert SECRET not in str(excinfo.value)

    def test_settings_normalise_the_url(self, settings_factory):
        settings = settings_factory(DATABASE_URL=f"postgres://u:{SECRET}@db/api")

        assert settings.DATABASE_URL == f"postgresql+psycopg://u:{SECRET}@db/api"

    def test_settings_refuse_asyncpg_without_echoing_the_url(self, settings_factory):
        with pytest.raises(ValidationError, match="asyncpg") as excinfo:
            settings_factory(DATABASE_URL=f"postgresql+asyncpg://u:{SECRET}@db/api")

        assert SECRET not in str(excinfo.value)

    def test_code_default_uses_psycopg(self, monkeypatch, settings_factory):
        monkeypatch.delenv("DATABASE_URL")

        assert settings_factory().DATABASE_URL.startswith("postgresql+psycopg://")


class TestEngineOptions:
    def test_engine_uses_psycopg_async(self, settings_factory):
        engine = database.build_engine(settings_factory())
        try:
            assert engine.dialect.name == "postgresql"
            assert engine.dialect.driver == "psycopg"
            assert engine.dialect.is_async
        finally:
            engine.sync_engine.dispose()

    def test_connect_timeout_comes_from_settings(self, monkeypatch, settings_factory):
        captured: dict = {}

        def _capture(url, **kwargs):
            captured.update(kwargs)
            return "engine"

        monkeypatch.setattr(database, "create_async_engine", _capture)

        database.build_engine(settings_factory(DB_CONNECT_TIMEOUT=7))

        assert captured["connect_args"] == {"connect_timeout": 7}
        assert captured["pool_pre_ping"] is True
        assert captured["hide_parameters"] is True

    def test_connect_timeout_has_a_floor(self, settings_factory):
        with pytest.raises(ValidationError):
            settings_factory(DB_CONNECT_TIMEOUT=0)


class TestEventLoopGuard:
    def test_selector_loop_is_accepted(self):
        async def _check() -> None:
            database.ensure_supported_event_loop()

        asyncio.run(_check(), loop_factory=LOOP_FACTORY)

    @pytest.mark.skipif(
        sys.platform != "win32", reason="ProactorEventLoop is Windows-only"
    )
    def test_proactor_loop_is_refused_with_launch_hints(self):
        async def _check() -> None:
            database.ensure_supported_event_loop()

        with pytest.raises(database.UnsupportedEventLoopError) as excinfo:
            asyncio.run(_check(), loop_factory=asyncio.ProactorEventLoop)

        message = str(excinfo.value)
        assert "python run.py dev" in message
        assert "fastapi dev" in message
