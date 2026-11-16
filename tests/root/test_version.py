# tests/root/test_version.py
"""Single version source: [project].version in pyproject.toml (review 2026-11, 1.1)."""

import tomllib
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from app.common import config
from app.common.config import Settings

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _project_version() -> str:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


@pytest.mark.root
def test_app_version_defaults_to_pyproject_version():
    s = Settings(_env_file=None, ENVIRONMENT="unittest")

    assert s.APP_VERSION == _project_version()


@pytest.mark.root
def test_openapi_reports_pyproject_version():
    from app.main import app

    assert app.openapi()["info"]["version"] == _project_version()


@pytest.mark.root
def test_missing_distribution_metadata_yields_visible_sentinel(monkeypatch):
    def _not_installed(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(config, "distribution_version", _not_installed)

    s = Settings(_env_file=None, ENVIRONMENT="unittest")

    assert s.APP_VERSION == "0.0.0+unknown"


@pytest.mark.root
def test_app_version_can_still_be_overridden_explicitly():
    s = Settings(_env_file=None, ENVIRONMENT="unittest", APP_VERSION="9.9.9")

    assert s.APP_VERSION == "9.9.9"
