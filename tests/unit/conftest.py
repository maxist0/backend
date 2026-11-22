"""Fixtures for the unit suite. Nothing here needs Docker.

Service addresses stay at the closed ports set in `tests/conftest.py`, so a
unit test that reaches for a database or Redis fails instead of hitting a
developer's own instance.
"""

from collections.abc import Iterator

import pytest

from app.core.config import get_settings
from app.core.logging import setup_logging
from tests.helpers import SettingsFactory, build_settings


@pytest.fixture(scope="session", autouse=True)
def configured_logging() -> None:
    """Configure logging once, like a process start.

    Code tested outside a lifespan logs through the structlog/stdlib chain,
    which `caplog` can only observe after `setup_logging`.
    """
    setup_logging(get_settings())


@pytest.fixture
def settings_factory() -> SettingsFactory:
    """Build `Settings` for one test: `settings_factory(APP_NAME="x")`.

    See `tests.helpers.build_settings`.
    """
    return build_settings


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Clear the `get_settings` cache before and after the test.

    Yields the `monkeypatch` so the test can change the environment. The
    teardown runs before `monkeypatch` restores the environment; the next
    `get_settings()` call then re-reads the restored test environment.
    """
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
