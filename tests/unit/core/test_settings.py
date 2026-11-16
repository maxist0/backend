"""`get_settings()`: explicit ENVIRONMENT, .env.local in dev only, caching."""

import pytest
from pydantic import ValidationError

from app.common.config import (
    ConfigurationError,
    Environment,
    Settings,
    get_settings,
)


class TestExplicitEnvironment:
    def test_missing_environment_is_refused(self, fresh_settings):
        fresh_settings.delenv("ENVIRONMENT")

        with pytest.raises(ConfigurationError, match="ENVIRONMENT is not set"):
            get_settings()

    def test_blank_environment_is_refused(self, fresh_settings):
        fresh_settings.setenv("ENVIRONMENT", "  ")

        with pytest.raises(ConfigurationError, match="ENVIRONMENT is not set"):
            get_settings()

    def test_unknown_environment_is_refused(self, fresh_settings):
        fresh_settings.setenv("ENVIRONMENT", "production")

        with pytest.raises(ConfigurationError, match="not valid"):
            get_settings()

    def test_settings_model_has_no_environment_default(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT")

        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_environment_comes_from_the_process(self, fresh_settings):
        """GUARD: the harness value is used and cached."""
        first = get_settings()

        assert first.ENVIRONMENT is Environment.UNITTEST
        assert get_settings() is first


class TestEnvLocal:
    """.env.local is read in dev only; real environment variables win."""

    def _write_env_local(self, directory, **values):
        lines = [f"{key}={value}" for key, value in values.items()]
        (directory / ".env.local").write_text("\n".join(lines), encoding="utf-8")

    def test_dev_reads_env_local(self, fresh_settings, tmp_path):
        self._write_env_local(tmp_path, APP_NAME="from-env-local")
        fresh_settings.chdir(tmp_path)
        fresh_settings.setenv("ENVIRONMENT", "dev")

        assert get_settings().APP_NAME == "from-env-local"

    def test_environment_variables_win_over_env_local(self, fresh_settings, tmp_path):
        self._write_env_local(tmp_path, APP_NAME="from-env-local")
        fresh_settings.chdir(tmp_path)
        fresh_settings.setenv("ENVIRONMENT", "dev")
        fresh_settings.setenv("APP_NAME", "from-process")

        assert get_settings().APP_NAME == "from-process"

    def test_env_local_cannot_change_the_environment(self, fresh_settings, tmp_path):
        self._write_env_local(tmp_path, ENVIRONMENT="prod")
        fresh_settings.chdir(tmp_path)
        fresh_settings.setenv("ENVIRONMENT", "dev")

        assert get_settings().ENVIRONMENT is Environment.DEV

    @pytest.mark.parametrize("environment", ["unittest", "test"])
    def test_other_environments_ignore_env_local(
        self, fresh_settings, tmp_path, environment
    ):
        self._write_env_local(tmp_path, APP_NAME="from-env-local")
        fresh_settings.chdir(tmp_path)
        fresh_settings.setenv("ENVIRONMENT", environment)
        # "test" is guarded: strong secrets, admin off.
        fresh_settings.setenv("JWT_SECRET_KEY", "k" * 48)
        fresh_settings.setenv("ADMIN_ENABLED", "false")

        assert get_settings().APP_NAME == "api"

    def test_template_env_file_is_never_read(self, fresh_settings, tmp_path):
        """`.env` is a template: Settings itself reads no dotenv file."""
        (tmp_path / ".env").write_text("APP_NAME=from-template\n", encoding="utf-8")
        fresh_settings.chdir(tmp_path)

        assert get_settings().APP_NAME == "api"
