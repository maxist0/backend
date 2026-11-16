# tests/common/test_database_driver.py
"""psycopg 3 as the only driver (review 2026-11, N1 / step 1.3).

URL normalisation, engine options, the Windows event-loop guard and
constraint names from psycopg's diagnostics on the async path.
"""

import asyncio
import os
import sys

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

import app.common.database as database
from app.api.auth.models import User
from app.common.config import Settings, normalize_database_url
from app.common.db_errors import constraint_name, find_db_error, sqlstate

SECRET = "pw-must-not-leak"


def _selector_loop_factory():
    """Loop factory psycopg can use on every platform (Windows: no Proactor)."""
    return asyncio.SelectorEventLoop if sys.platform == "win32" else None


def _settings(**overrides) -> Settings:
    values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
    return Settings(_env_file=None, **values)


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

    def test_settings_normalise_the_url(self):
        settings = _settings(DATABASE_URL=f"postgres://u:{SECRET}@db/api")

        assert settings.DATABASE_URL == f"postgresql+psycopg://u:{SECRET}@db/api"

    def test_settings_refuse_asyncpg_without_echoing_the_url(self):
        with pytest.raises(ValidationError, match="asyncpg") as excinfo:
            _settings(DATABASE_URL=f"postgresql+asyncpg://u:{SECRET}@db/api")

        assert SECRET not in str(excinfo.value)

    def test_code_default_uses_psycopg(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL")

        assert _settings().DATABASE_URL.startswith("postgresql+psycopg://")


class TestEngineOptions:
    def test_engine_uses_psycopg_async(self):
        engine = database.build_engine(_settings())
        try:
            assert engine.dialect.name == "postgresql"
            assert engine.dialect.driver == "psycopg"
            assert engine.dialect.is_async
        finally:
            engine.sync_engine.dispose()

    def test_connect_timeout_comes_from_settings(self, monkeypatch):
        captured: dict = {}

        def _capture(url, **kwargs):
            captured.update(kwargs)
            return "engine"

        monkeypatch.setattr(database, "create_async_engine", _capture)

        database.build_engine(_settings(DB_CONNECT_TIMEOUT=7))

        assert captured["connect_args"] == {"connect_timeout": 7}
        assert captured["pool_pre_ping"] is True
        assert captured["hide_parameters"] is True

    def test_connect_timeout_has_a_floor(self):
        with pytest.raises(ValidationError):
            _settings(DB_CONNECT_TIMEOUT=0)


class TestEventLoopGuard:
    def test_selector_loop_is_accepted(self):
        async def _check() -> None:
            database.ensure_supported_event_loop()

        asyncio.run(_check(), loop_factory=_selector_loop_factory())

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


class TestPsycopgConstraintNames:
    def test_unique_violation_reports_constraint_and_sqlstate(self):
        """GUARD: the register conflict mapping relies on these names (async psycopg)."""

        async def _duplicate_email() -> IntegrityError:
            engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
            try:
                async with AsyncSession(engine) as session:
                    session.add(User(username="first_user", email="dup@example.com"))
                    await session.commit()
                    session.add(User(username="second_user", email="dup@example.com"))
                    with pytest.raises(IntegrityError) as excinfo:
                        await session.commit()
                    return excinfo.value
            finally:
                await engine.dispose()

        exc = asyncio.run(_duplicate_email(), loop_factory=_selector_loop_factory())

        assert type(exc.orig).__module__.startswith("psycopg")
        try:
            raise RuntimeError("wrapper") from exc
        except RuntimeError as wrapper:
            assert find_db_error(wrapper) is exc
        assert constraint_name(exc) == "users_email_key"
        assert sqlstate(exc) == "23505"
