"""Fixtures for the unit suite. Nothing here needs Docker.

Service addresses stay at the closed ports set in `tests/conftest.py`, so a
unit test that reaches for a database or Redis fails instead of hitting a
developer's own instance.
"""

from collections.abc import Callable, Iterator

import pytest

from app.common.config import Settings, get_settings
from app.common.logging import setup_logging


@pytest.fixture(scope="session", autouse=True)
def configured_logging() -> None:
    """Configure logging once, like a process start.

    Code tested outside a lifespan logs through the structlog/stdlib chain,
    which `caplog` can only observe after `setup_logging`.
    """
    setup_logging(get_settings())


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Build `Settings` for one test: `settings_factory(APP_NAME="x")`.

    The result ignores dotenv files. `ENVIRONMENT` defaults to `unittest` and
    the admin panel to off (sqladmin keeps the session factory on its view
    classes, so a second admin-enabled app would rebind the shared views).
    Every default can be overridden, `None` included.
    """

    def _build(**overrides: object) -> Settings:
        values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
        return Settings(_env_file=None, **values)

    return _build


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
