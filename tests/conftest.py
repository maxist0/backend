"""Process-wide test setup shared by the unit and integration suites.

This module only prepares the environment that must exist before the first
`app` import and marks every test by the folder it lives in. Fixtures live in
`tests/unit/conftest.py` (no Docker) and `tests/integration/conftest.py`
(Postgres and Redis containers). A module's own tests bring their own
`conftest.py`; this file is never edited for a new module.
"""

import os
from pathlib import Path

import pytest

# ENVIRONMENT is required (no fallback to dev), and "unittest" never reads
# .env.local.
os.environ["ENVIRONMENT"] = "unittest"
# The admin panel is opt-in outside "dev". The admin tests drive the real
# sqladmin app, so enable it explicitly for the test process.
os.environ["ADMIN_ENABLED"] = "true"
# Collection imports app modules (`app.main` builds an app) before any
# container exists and its port is known. Until the integration
# `test_environment` fixture wires the real addresses, every service address
# points at a closed port, so nothing imported early can reach a developer's
# own Postgres or Redis on the default ports. Unit tests keep these addresses
# for the whole run: they never connect. In the integration suite the app
# lifespan builds the taskiq broker from the wired settings; tests that
# register users use `captured_emails`, so no mail task is enqueued.
_UNREACHABLE = "127.0.0.1:1"
os.environ["DATABASE_URL"] = f"postgresql+psycopg://x:x@{_UNREACHABLE}/unconfigured"
os.environ["CACHE_HOST"] = "127.0.0.1"
os.environ["CACHE_PORT"] = "1"
os.environ["TASKIQ_BROKER_URL"] = f"redis://{_UNREACHABLE}/1"
os.environ["TASKIQ_RESULT_BACKEND_URL"] = f"redis://{_UNREACHABLE}/2"

_TESTS_DIR = Path(__file__).resolve().parent
_MARKED_FOLDERS = {
    "unit": _TESTS_DIR / "unit",
    "integration": _TESTS_DIR / "integration",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark each test `unit` or `integration` by its folder.

    A test outside both folders is a layout error and stops the run, so no test
    can silently skip the Docker boundary the folders stand for.
    """
    for item in items:
        for marker, folder in _MARKED_FOLDERS.items():
            if folder in item.path.parents:
                item.add_marker(getattr(pytest.mark, marker))
                break
        else:
            raise pytest.UsageError(
                f"{item.nodeid} is in neither tests/unit nor tests/integration"
            )
