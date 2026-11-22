"""Single version source: [project].version in pyproject.toml."""

import tomllib
from importlib.metadata import PackageNotFoundError

import pytest

from app.core import config
from app.core.config import Environment, Settings
from tests.helpers import BACKEND_DIR

PYPROJECT = BACKEND_DIR / "pyproject.toml"


def _project_version() -> str:
    with PYPROJECT.open("rb") as f:
        version: str = tomllib.load(f)["project"]["version"]
    return version


@pytest.mark.root
def test_app_version_defaults_to_pyproject_version() -> None:
    s = Settings(ENVIRONMENT=Environment.UNITTEST)

    assert _project_version() == s.APP_VERSION


@pytest.mark.root
def test_openapi_reports_pyproject_version() -> None:
    from app.main import app

    assert app.openapi()["info"]["version"] == _project_version()


@pytest.mark.root
def test_missing_distribution_metadata_yields_visible_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(config, "distribution_version", _not_installed)

    s = Settings(ENVIRONMENT=Environment.UNITTEST)

    assert s.APP_VERSION == "0.0.0+unknown"


@pytest.mark.root
def test_app_version_can_still_be_overridden_explicitly() -> None:
    s = Settings(ENVIRONMENT=Environment.UNITTEST, APP_VERSION="9.9.9")

    assert s.APP_VERSION == "9.9.9"
