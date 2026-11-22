"""The tracked `.env` template, copied to `.env.local`, is a startable dev config."""

import shutil
from pathlib import Path

import pytest

from app.core.config import Environment, Settings, get_settings
from app.main import create_app
from tests.helpers import BACKEND_DIR

TEMPLATE = BACKEND_DIR / ".env"


@pytest.fixture
def template_as_env_local(
    fresh_settings: pytest.MonkeyPatch, tmp_path: Path
) -> pytest.MonkeyPatch:
    """Run the test like `cp .env .env.local` followed by `run.py dev`.

    Every settings variable is removed from the process environment, so the
    values come from the copied template or the code defaults only, not from
    the test harness or the developer's shell.
    """
    shutil.copyfile(TEMPLATE, tmp_path / ".env.local")
    fresh_settings.chdir(tmp_path)
    for name in Settings.model_fields:
        fresh_settings.delenv(name, raising=False)
    fresh_settings.setenv("ENVIRONMENT", Environment.DEV.value)
    return fresh_settings


@pytest.mark.usefixtures("template_as_env_local")
class TestEnvTemplate:
    def test_a_copy_as_env_local_builds_the_app(self) -> None:
        """`create_app()` performs no I/O, so no service has to run."""
        settings = get_settings()

        app = create_app(settings)

        assert settings.ENVIRONMENT is Environment.DEV
        assert app.title == "api"
        assert app.openapi()["info"]["title"] == "api"

    def test_a_copy_as_env_local_keeps_every_default(self) -> None:
        """Every active line holds the code default; unset stays unset.

        An empty `KEY=` is an empty string, not "unset", and a fixed value
        such as `APP_VERSION` would go stale. Only the names of differing
        settings are reported, never their values.
        """
        from_template = get_settings().model_dump()
        defaults = Settings(ENVIRONMENT=Environment.DEV).model_dump()

        changed = sorted(k for k in defaults if from_template[k] != defaults[k])

        assert changed == []
