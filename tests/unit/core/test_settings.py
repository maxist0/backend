"""`get_settings()`: explicit ENVIRONMENT, .env.local in dev only, caching."""

import pytest
from pydantic import ValidationError

from app.core.config import (
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
        # "test" is guarded: strong secrets, https frontend, admin off.
        fresh_settings.setenv("JWT_SECRET_KEY", "k" * 48)
        fresh_settings.setenv("FRONTEND_URL", "https://app.example.com")
        fresh_settings.setenv("ADMIN_ENABLED", "false")

        assert get_settings().APP_NAME == "api"

    def test_template_env_file_is_never_read(self, fresh_settings, tmp_path):
        """`.env` is a template: Settings itself reads no dotenv file."""
        (tmp_path / ".env").write_text("APP_NAME=from-template\n", encoding="utf-8")
        fresh_settings.chdir(tmp_path)

        assert get_settings().APP_NAME == "api"


class TestCorsOrigins:
    def test_default_is_the_local_frontend(self, settings_factory):
        assert settings_factory().CORS_ORIGINS == ["http://localhost:5173"]

    def test_read_from_the_environment_as_a_json_list(self, fresh_settings):
        fresh_settings.setenv(
            "CORS_ORIGINS", '["https://app.example.com", "http://localhost:3000"]'
        )

        assert get_settings().CORS_ORIGINS == [
            "https://app.example.com",
            "http://localhost:3000",
        ]

    @pytest.mark.parametrize("environment", ["dev", "unittest", "prod"])
    def test_wildcard_is_refused_in_every_environment(
        self, settings_factory, environment
    ):
        with pytest.raises(ValidationError, match="must not contain"):
            settings_factory(
                ENVIRONMENT=environment,
                JWT_SECRET_KEY="k" * 48,
                CORS_ORIGINS=["https://app.example.com", "*"],
            )

    @pytest.mark.parametrize(
        "origin",
        ["https://app.example.com/", "https://app.example.com/path", "null", "app"],
    )
    def test_values_that_are_not_origins_are_refused(self, settings_factory, origin):
        with pytest.raises(ValidationError, match="is not an origin"):
            settings_factory(CORS_ORIGINS=[origin])

    def test_the_app_uses_the_configured_origins(self, settings_factory):
        from app.main import create_app
        from tests.helpers import new_test_client

        app = create_app(settings_factory(CORS_ORIGINS=["https://app.example.com"]))
        client = new_test_client(app)

        allowed = client.get("/health", headers={"Origin": "https://app.example.com"})
        other = client.get("/health", headers={"Origin": "http://localhost:5173"})
        preflight = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

        assert allowed.headers["access-control-allow-origin"] == (
            "https://app.example.com"
        )
        assert allowed.headers["access-control-allow-credentials"] == "true"
        assert "access-control-allow-origin" not in other.headers
        assert preflight.status_code == 200


class TestProxyAndLockoutSettings:
    def test_forwarded_allow_ips_defaults_to_localhost(self, settings_factory):
        assert settings_factory().FORWARDED_ALLOW_IPS == "127.0.0.1"

    def test_lockout_defaults(self, settings_factory):
        settings = settings_factory()

        assert (settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_LOCKOUT_SECONDS) == (5, 900)

    @pytest.mark.parametrize(
        "override",
        [
            {"LOGIN_MAX_ATTEMPTS": 0},
            {"LOGIN_MAX_ATTEMPTS": 101},
            {"LOGIN_LOCKOUT_SECONDS": 0},
            {"LOGIN_LOCKOUT_SECONDS": 86_401},
        ],
        ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items()),
    )
    def test_values_outside_the_bounds_are_rejected(self, settings_factory, override):
        with pytest.raises(ValidationError):
            settings_factory(**override)

    def test_the_bounds_themselves_are_accepted(self, settings_factory):
        """GUARD (control): the rejections above are caused by the bound only."""
        low = settings_factory(LOGIN_MAX_ATTEMPTS=1, LOGIN_LOCKOUT_SECONDS=1)
        high = settings_factory(LOGIN_MAX_ATTEMPTS=100, LOGIN_LOCKOUT_SECONDS=86_400)

        assert (low.LOGIN_MAX_ATTEMPTS, high.LOGIN_LOCKOUT_SECONDS) == (1, 86_400)

    def test_removed_settings_are_gone(self):
        for name in ("DEBUG", "ROUTE_CACHE_ENABLED", "OTLP_LOGS_ENABLED"):
            assert name not in Settings.model_fields


class TestCleanupRetention:
    def test_default_is_thirty_days(self, settings_factory):
        assert settings_factory().AUTH_CLEANUP_RETENTION_DAYS == 30

    @pytest.mark.parametrize("days", [0, 366, -1])
    def test_values_outside_the_bounds_are_rejected(self, settings_factory, days):
        with pytest.raises(ValidationError):
            settings_factory(AUTH_CLEANUP_RETENTION_DAYS=days)

    @pytest.mark.parametrize("days", [1, 365])
    def test_the_bounds_themselves_are_accepted(self, settings_factory, days):
        """GUARD (control): the rejections above are caused by the bound only."""
        assert (
            settings_factory(
                AUTH_CLEANUP_RETENTION_DAYS=days
            ).AUTH_CLEANUP_RETENTION_DAYS
            == days
        )
